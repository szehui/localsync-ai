"""The request write-path: ask, approve, push to Lidarr, retry.

Availability is decided by release-group MBID at every step. A request names one
specific release group and Lidarr is asked to resolve *that* MBID
(`album/lookup?term=mbid:<uuid>`); the fuzzy title picker is never used here.
That matters because title lookup resolves compilations and non-Latin releases
to the wrong album — which for a request means downloading something the user
never asked for while the UI says "approved".

Status is a state machine shared with the reconciler (services/request_sync.py):

    pending ──approve──> approved ──> downloading ──> imported
                                             └─────> partially_available
                                                     └──> available
    pending/approved ──deny──> denied        (owner only)
    pending ──cancel──> cancelled            (requester or owner)

``denied`` and ``cancelled`` are the only terminal states that free the
release group for a new request; every other state keeps the partial unique
index occupied, so a second user asking for the same album becomes a vote on
the existing row rather than a duplicate Lidarr add.

A transient Lidarr failure never becomes ``failed``: the request stays
``approved`` with ``last_error`` set and is retried, because a 503 from Lidarr's
MusicBrainz proxy is throttling, not a verdict.
"""
import logging
from datetime import datetime
from typing import Iterable, Optional

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    Album,
    Blocklist,
    LidarrConfig,
    Request,
    RequestVote,
    User,
)
from app.services.catalog_cache import get as cache_get
from app.services.catalog_cache import make_key, put as cache_put
from app.services.coverart import front_covers
from app.services.musicbrainz import (
    MusicBrainzClient,
    artist_credit_name,
    artist_mbid,
    first_release_year,
    is_requestable,
)

logger = logging.getLogger(__name__)

# Every status except the two below holds the partial unique index open. Keeping
# this as a negative list means the index predicate in models/database.py and the
# service agree by construction: adding a status cannot silently let a duplicate
# through.
CLOSED_STATUSES = ("denied", "cancelled")

# The positive complement, for readers that need "is this still in flight?".
# Terminal-in-truth `denied`/`cancelled`/`failed` are excluded, and so is
# `available` (the album is in the library, so a card shows it as owned rather
# than as someone's open request). Derived rather than written out, so it cannot
# drift from CLOSED_STATUSES.
ALL_STATUSES = ("pending", "approved", "downloading", "partially_available",
                "imported", "available", "failed", "denied", "cancelled")
OPEN_STATUSES = tuple(
    s for s in ALL_STATUSES
    if s not in CLOSED_STATUSES and s not in ("available", "failed")
)

# How far forward a request may travel. The reconciler will not move a row
# backwards: an `available` album that Lidarr briefly forgets is still in the
# library, and a `downloading` row must not fall back to `approved` just because
# the queue listing was empty for one cycle (Lidarr swaps records between the
# queue and the importer). `failed` is handled separately — see `_may_advance`.
STATUS_RANK = {
    "pending": 0,
    "approved": 1,
    "downloading": 2,
    "partially_available": 3,
    "imported": 4,
    "available": 5,
}

# A single artist request expanding to hundreds of albums is either a mistake or
# a data problem (a bogus via-alias). 100 is generous for a discography and small
# enough that the expansion is reviewable before approval.
MAX_ARTIST_ALBUMS = 100

# Artist request scopes. "studio" deliberately excludes soundtracks and
# compilations: those are legitimate albums but requesting them as part of "the
# discography" is not what anyone means.
SCOPES = {
    "studio": ("album",),
    "studio_eps": ("album", "ep"),
    "everything": ("album", "ep", "single"),
}


class RequestError(Exception):
    """A request that must be refused, with the reason the UI should show.

    ``code`` is stable so the frontend can branch on it; ``message`` is prose.
    """

    def __init__(self, message: str, *, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ── Plumbing ────────────────────────────────────────────────────────────────


def _lidarr_service(db: Session):
    """The shared Lidarr client, or one built from saved config."""
    import app.main as _main

    from app.services.lidarr import LidarrService

    if _main.lidarr_service is not None:
        return _main.lidarr_service

    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config:
        _main.set_lidarr_service(LidarrService(config.url, config.api_key))
        return _main.lidarr_service
    return None


def _user_row(db: Session, user) -> User:
    """Load the caller's row. Callers pass the JWT dict, not the ORM object."""
    uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    row = db.get(User, uid) if uid is not None else None
    if row is None:
        raise RequestError("Unknown user", code="unknown_user", status=401)
    return row


def is_open(request: Request) -> bool:
    return request.status not in CLOSED_STATUSES


def open_request_for(db: Session, release_group_mbid: str) -> Optional[Request]:
    return (
        db.query(Request)
        .filter(Request.kind == "album",
                Request.release_group_mbid == release_group_mbid,
                Request.status.notin_(CLOSED_STATUSES))
        .first()
    )


def _blocked(db: Session, *, release_group_mbid: Optional[str],
             artist_mbid_: Optional[str]) -> Optional[Blocklist]:
    """Return the blocking row, if this album or its artist is blocklisted.

    The query lives in ``services.blocklist`` so the catalog's filter and this
    refusal check cannot disagree about what "blocked" means.
    """
    from app.services import blocklist as blocklist_svc

    return blocklist_svc.blocked_rows(
        db, release_group_mbid=release_group_mbid, artist_mbid_=artist_mbid_)


def _owned_by_mbid(db: Session, release_group_mbid: str) -> bool:
    """True when the library already holds this exact release group.

    Only the MBID tier is used here. A folded name match is a *hint* the UI
    shows as "probably in your library" — refusing a request on it would block
    legitimate asks for albums that merely share a title (live albums, remixes,
    same-named EPs by other artists).
    """
    return db.query(Album.id).filter(
        Album.release_group_mbid == release_group_mbid).first() is not None


async def fetch_release_group(db: Session, release_group_mbid: str, *,
                              mb: Optional[MusicBrainzClient] = None) -> dict:
    """Release-group payload from cache, else MusicBrainz.

    A request stores the display fields it needs, so approval and the request
    list never need upstream calls. This is the one place that pays for them.
    """
    key = make_key("album", release_group_mbid)
    cached = cache_get(db, key, "album")
    if cached:
        return cached

    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        detail = await client.get_release_group(
            release_group_mbid, inc="artist-credits+releases")
    except httpx.HTTPStatusError as exc:
        # MusicBrainz answers 400 — not 404 — for a well-formed UUID it has never
        # heard of, so the client's allow_404 flag does not cover it. Both mean
        # the same thing to us: nothing to request. Without this the endpoint
        # 500s on a typo'd or fabricated MBID.
        if 400 <= exc.response.status_code < 500:
            raise RequestError(
                f"MusicBrainz has no release group {release_group_mbid}",
                code="not_found", status=404) from exc
        raise RequestError(
            "MusicBrainz is unavailable right now; try again shortly.",
            code="upstream", status=503) from exc
    except httpx.HTTPError as exc:
        raise RequestError(
            "Could not reach MusicBrainz; try again shortly.",
            code="upstream", status=503) from exc
    finally:
        if owns_client:
            await client.close()

    if not detail:
        raise RequestError(
            f"MusicBrainz has no release group {release_group_mbid}",
            code="not_found", status=404)
    cache_put(db, key, "album", detail)
    return detail


# ── Voting ──────────────────────────────────────────────────────────────────


def add_vote(db: Session, request: Request, user, *, commit: bool = True) -> bool:
    """Record this user's interest. Idempotent; returns True when newly added.

    The composite primary key is the idempotency guarantee — a double tap and a
    genuine race both surface as IntegrityError, which is treated as success.
    """
    row = _user_row(db, user)
    db.add(RequestVote(request_id=request.id, user_id=row.id))
    try:
        if commit:
            db.commit()
        else:
            db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False


def vote_count(db: Session, request_id: int) -> int:
    return db.query(RequestVote).filter(
        RequestVote.request_id == request_id).count()


def vote_counts(db: Session, request_ids: Iterable[int]) -> dict[int, int]:
    """Vote tallies for a page of requests, in one query."""
    ids = [i for i in request_ids if i is not None]
    if not ids:
        return {}
    rows = (
        db.query(RequestVote.request_id, RequestVote.user_id)
        .filter(RequestVote.request_id.in_(ids))
        .all()
    )
    counts: dict[int, int] = {}
    for request_id, _ in rows:
        counts[request_id] = counts.get(request_id, 0) + 1
    return counts


# ── Creation ────────────────────────────────────────────────────────────────


async def create_album_request(db: Session, user, release_group_mbid: str, *,
                               note: Optional[str] = None,
                               mb: Optional[MusicBrainzClient] = None) -> dict:
    """Ask for one album. Returns ``{request, created, voted}``.

    A second ask for an album that already has an open request becomes a vote on
    that request — never a second row, because a second row would mean a second
    Lidarr add of the same release group.
    """
    row = _user_row(db, user)
    if not row.can_request:
        raise RequestError(
            "Your account is not allowed to make requests",
            code="cannot_request", status=403)

    release_group_mbid = (release_group_mbid or "").strip()
    if not release_group_mbid:
        raise RequestError("release_group_mbid is required", code="missing_mbid")

    detail = await fetch_release_group(db, release_group_mbid, mb=mb)
    if not is_requestable(detail):
        primary = (detail.get("primary-type") or "unknown").lower()
        raise RequestError(
            f"{primary.title()} releases are not requestable",
            code="not_requestable")

    if _blocked(db, release_group_mbid=release_group_mbid,
                artist_mbid_=artist_mbid(detail)):
        raise RequestError("This album is blocked", code="blocked", status=403)

    if _owned_by_mbid(db, release_group_mbid):
        raise RequestError("You already have this album", code="already_owned",
                           status=409)

    existing = open_request_for(db, release_group_mbid)
    if existing is not None:
        voted = add_vote(db, existing, user)
        return {"request": existing, "created": False, "voted": voted}

    request = Request(
        user_id=row.id,
        kind="album",
        release_group_mbid=release_group_mbid,
        artist_mbid=artist_mbid(detail),
        artist_name=artist_credit_name(detail),
        album_title=detail.get("title"),
        year=first_release_year(detail),
        primary_type=detail.get("primary-type"),
        cover_url=await _cover(db, release_group_mbid),
        status="pending",
        note=note,
    )
    db.add(request)
    try:
        db.commit()
    except IntegrityError:
        # Another request landed between the check above and this insert. The
        # unique index is the real guard; convert the race into a vote.
        db.rollback()
        existing = open_request_for(db, release_group_mbid)
        if existing is None:
            raise RequestError("Could not create the request", code="conflict",
                               status=409)
        voted = add_vote(db, existing, user)
        return {"request": existing, "created": False, "voted": voted}

    db.refresh(request)
    # The requester implicitly wants it; a later "me too" from them is a no-op.
    add_vote(db, request, user)

    if row.auto_approve:
        await approve(db, request, user)

    db.refresh(request)
    return {"request": request, "created": True, "voted": True}


async def _cover(db: Session, release_group_mbid: str) -> Optional[str]:
    """Artwork for the request row, best-effort.

    Stored so the request list renders without a catalog call. CAA 404s often
    and must never fail a request.
    """
    try:
        covers = await front_covers(db, [release_group_mbid])
        return covers.get(release_group_mbid)
    except Exception:  # noqa: BLE001 — artwork is decoration
        logger.warning("cover art fetch failed for %s", release_group_mbid,
                       exc_info=True)
        return None


# ── Lifecycle ───────────────────────────────────────────────────────────────


def _may_decide(actor_row: User, request: Request) -> bool:
    """Owners decide everything; anyone else only their own auto-approved ask.

    ``auto_approve`` is what lets a trusted user skip the queue, so it must also
    let them push their own request through later — otherwise the flag only works
    on the exact request that happened to be created after it was set.
    """
    if actor_row.is_owner:
        return True
    return request.user_id == actor_row.id and bool(actor_row.auto_approve)


async def approve(db: Session, request: Request, actor) -> Request:
    """Approve and push to Lidarr.

    Album requests push their own MBID. Artist requests approve their children
    and push every child MBID in ONE call, so monitoring collapses once for the
    whole discography instead of once per album.
    """
    actor_row = _user_row(db, actor)
    if not _may_decide(actor_row, request):
        raise RequestError("Only the owner can approve requests",
                           code="forbidden", status=403)

    if not is_open(request):
        raise RequestError(
            f"Request is already {request.status}", code="not_open", status=409)

    service = _lidarr_service(db)
    if service is None:
        raise RequestError("Lidarr is not configured", code="no_lidarr",
                           status=503)

    request.status = "approved"
    request.decided_at = request.decided_at or datetime.utcnow()
    request.decided_by = actor_row.id
    db.commit()

    if request.kind == "artist":
        children = (
            db.query(Request)
            .filter(Request.parent_request_id == request.id,
                    Request.status.notin_(CLOSED_STATUSES))
            .all()
        )
        mbids = [c.release_group_mbid for c in children if c.release_group_mbid]
        targets = children
    else:
        mbids = [request.release_group_mbid] if request.release_group_mbid else []
        targets = [request]

    await _push(db, service, targets, mbids)

    if request.kind == "artist":
        # The parent has no Lidarr album of its own; it inherits the children's
        # ids so the reconciler can roll their state up to it.
        ids = {t.lidarr_album_id for t in targets if t.lidarr_album_id}
        request.lidarr_album_id = next(iter(ids), None) if len(ids) == 1 else None
        request.lidarr_artist_id = next(
            (t.lidarr_artist_id for t in targets if t.lidarr_artist_id), None)

    db.commit()
    db.refresh(request)
    return request


async def _push(db: Session, service, targets: list[Request],
                mbids: list[str]) -> None:
    """Send MBIDs to Lidarr and record the outcome on each request row.

    A transport failure leaves the request ``approved`` with ``last_error`` — it
    is retryable, and marking it ``failed`` would tell the user their album is
    impossible when Lidarr was merely throttling.
    """
    if not mbids:
        for target in targets:
            target.last_error = "No release group MBID to add"
        db.commit()
        return

    try:
        outcome = await service.add_albums_by_mbid(mbids)
    except Exception as exc:  # noqa: BLE001 — transport-level
        logger.warning("Lidarr push failed: %s", exc)
        for target in targets:
            target.last_error = str(exc)
            target.last_synced_at = datetime.utcnow()
        db.commit()
        return

    by_mbid = {}
    for result in outcome.get("results", []):
        by_mbid[result.get("release_group_mbid")] = result

    for target in targets:
        result = by_mbid.get(target.release_group_mbid)
        target.last_synced_at = datetime.utcnow()
        if result is None:
            continue
        if result.get("error"):
            # Unresolved is a dead end ("Lidarr has no album for this release
            # group"), a transport error is not; both keep the row approved so
            # retry can distinguish them by their message.
            target.last_error = result["error"]
        elif result.get("album_id"):
            target.lidarr_album_id = result["album_id"]
            target.lidarr_artist_id = result.get("artist_id")
            target.last_error = None
    db.commit()


def deny(db: Session, request: Request, actor, reason: Optional[str] = None) -> Request:
    """Refuse a request. Owner only. Frees the release group for a later ask."""
    actor_row = _user_row(db, actor)
    if not actor_row.is_owner:
        raise RequestError("Only the owner can deny requests",
                           code="forbidden", status=403)
    if not is_open(request):
        raise RequestError(
            f"Request is already {request.status}", code="not_open", status=409)

    request.status = "denied"
    request.decided_at = datetime.utcnow()
    request.decided_by = actor_row.id
    if reason:
        request.note = (request.note + "\n" if request.note else "") + f"Denied: {reason}"
    db.commit()
    db.refresh(request)
    return request


def cancel(db: Session, request: Request, actor) -> Request:
    """Withdraw a request. The requester or the owner, and only while pending.

    Cancelling after approval would leave the album sitting in Lidarr's wanted
    list with nothing tracking it, so it is refused rather than half-done.
    """
    actor_row = _user_row(db, actor)
    if request.user_id != actor_row.id and not actor_row.is_owner:
        raise RequestError("You can only cancel your own requests",
                           code="forbidden", status=403)
    if request.status != "pending":
        raise RequestError(
            f"Only pending requests can be cancelled (this one is {request.status})",
            code="not_pending", status=409)

    request.status = "cancelled"
    request.decided_at = datetime.utcnow()
    request.decided_by = actor_row.id
    db.commit()
    db.refresh(request)
    return request


async def retry(db: Session, request: Request, actor) -> Request:
    """Re-resolve and re-push a request that failed. Owner only.

    Keeps the same row: the partial unique index would otherwise refuse a new
    request for the same release group while this one still holds it, and the
    history of a failed attempt is worth keeping.
    """
    actor_row = _user_row(db, actor)
    if not actor_row.is_owner:
        raise RequestError("Only the owner can retry requests",
                           code="forbidden", status=403)
    if request.status in CLOSED_STATUSES:
        raise RequestError(
            f"A {request.status} request cannot be retried", code="closed",
            status=409)

    service = _lidarr_service(db)
    if service is None:
        raise RequestError("Lidarr is not configured", code="no_lidarr",
                           status=503)

    request.last_error = None
    request.status = "approved"
    # A retry is a fresh attempt, so a second failure deserves its own
    # notification rather than being suppressed by the first one's stamp.
    request.notified_status = None
    db.commit()

    if request.kind == "artist":
        children = (
            db.query(Request)
            .filter(Request.parent_request_id == request.id,
                    Request.status.notin_(CLOSED_STATUSES))
            .all()
        )
        mbids = [c.release_group_mbid for c in children if c.release_group_mbid]
        targets = children
    else:
        mbids = [request.release_group_mbid] if request.release_group_mbid else []
        targets = [request]

    await _push(db, service, targets, mbids)
    db.commit()
    db.refresh(request)
    return request


# ── Discovery offers → requests ─────────────────────────────────────────────


async def resolve_offer_release_group(
    db: Session, *, release_mbid: Optional[str], artist_name: Optional[str],
    release_name: Optional[str], mb: Optional[MusicBrainzClient] = None,
) -> Optional[dict]:
    """Find the release group a discovery offer refers to.

    Two routes, cheapest first:

    1. The offer carries a *release* MBID, which resolves exactly to its release
       group. No guessing.
    2. No MBID: search, then require the artist to agree and the title to fold
       to the same thing. A search hit alone is not enough — MusicBrainz ranks
       unrelated titles highly, and adding a stranger's album is the exact
       failure this path exists to prevent.

    Returns the release-group payload, or None when nothing is trustworthy.
    """
    from app.services.title_norm import fold, norm_title

    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        if release_mbid:
            release = await client.get_release(release_mbid)
            group = (release or {}).get("release-group") or {}
            if group.get("id"):
                detail = await client.get_release_group(group["id"],
                                                        inc="artist-credits")
                if detail:
                    return detail

        if not artist_name or not release_name:
            return None

        wanted_artist = fold(artist_name)
        wanted_title = norm_title(release_name)
        results = await client.search_release_groups(
            f'artist:"{artist_name}" AND releasegroup:"{release_name}"', limit=10)
        for rg in results.get("release_groups", []) if isinstance(results, dict) else []:
            if fold(artist_credit_name(rg)) != wanted_artist:
                continue
            if norm_title(rg.get("title") or "") != wanted_title:
                continue
            return rg
        return None
    finally:
        if owns_client:
            await client.close()


async def create_request_from_offer(db: Session, user, offer, *,
                                    note: Optional[str] = None,
                                    mb: Optional[MusicBrainzClient] = None) -> Optional[dict]:
    """Turn a discovery offer into an album request.

    Returns None when the offer cannot be resolved to a release group — the
    caller reports that instead of adding something nearby, which is what the
    old direct-to-Lidarr path did (it resolved by title and could pick a
    different album by the same artist).
    """
    detail = await resolve_offer_release_group(
        db,
        release_mbid=getattr(offer, "release_mbid", None),
        artist_name=getattr(offer, "artist_name", None),
        release_name=getattr(offer, "release_name", None),
        mb=mb,
    )
    if detail is None:
        return None

    release_group_mbid = detail.get("id")
    if not release_group_mbid:
        return None

    return await create_album_request(db, user, release_group_mbid, note=note)


# ── Artist discographies ────────────────────────────────────────────────────


async def plan_artist_request(
    db: Session, artist_mbid_: str, *, scope: str = "studio",
    mb: Optional[MusicBrainzClient] = None,
) -> dict:
    """Work out what a discography request would ask for, without creating it.

    The UI shows this before confirming ("17 albums; 3 already in your library"),
    so the user approves a count they have actually seen.
    """
    if scope not in SCOPES:
        raise RequestError(f"Unknown scope {scope!r}", code="bad_scope")

    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        artist = await client.get_artist(artist_mbid_)
        if not artist:
            raise RequestError(
                f"MusicBrainz has no artist {artist_mbid_}", code="not_found",
                status=404)

        release_groups: list[dict] = []
        for primary in SCOPES[scope]:
            release_groups.extend(
                await client.browse_artist_release_groups(artist_mbid_, type_=primary))
    finally:
        if owns_client:
            await client.close()

    wanted, owned, already, blocked = [], 0, 0, 0
    seen: set[str] = set()
    for rg in release_groups:
        rgid = rg.get("id")
        if not rgid or rgid in seen:
            continue
        seen.add(rgid)
        if not is_requestable(rg):
            continue
        if scope == "studio" and (rg.get("secondary-types") or []):
            continue
        if _blocked(db, release_group_mbid=rgid, artist_mbid_=None):
            blocked += 1
            continue
        if _owned_by_mbid(db, rgid):
            owned += 1
            continue
        if open_request_for(db, rgid) is not None:
            already += 1
            continue
        wanted.append({
            "release_group_mbid": rgid,
            "title": rg.get("title"),
            "year": first_release_year(rg),
            "primary_type": rg.get("primary-type"),
        })

    if len(wanted) > MAX_ARTIST_ALBUMS:
        raise RequestError(
            f"{len(wanted)} albums is too many for one request "
            f"(limit {MAX_ARTIST_ALBUMS}); request a few at a time",
            code="too_many", status=422)

    return {
        "artist_mbid": artist.get("id"),
        "artist_name": artist.get("name"),
        "disambiguation": artist.get("disambiguation"),
        "scope": scope,
        "albums": wanted,
        "summary": {
            "requestable": len(wanted),
            "owned": owned,
            "already_requested": already,
            "blocked": blocked,
            "total": len(wanted) + owned + already + blocked,
        },
    }


async def create_artist_request(db: Session, user, artist_mbid_: str, *,
                                scope: str = "studio",
                                note: Optional[str] = None,
                                mb: Optional[MusicBrainzClient] = None) -> dict:
    """Ask for part of a discography, expanded into child album requests.

    Deliberately NOT `POST /artist` with `monitor: "all"`: that monitors the
    entire discography, and a background RefreshArtist keeps re-monitoring it.
    Per-album children keep the collapse-once-per-artist contract, give per-album
    status, and isolate one bad album from the rest.
    """
    row = _user_row(db, user)
    if not row.can_request:
        raise RequestError("Your account is not allowed to make requests",
                           code="cannot_request", status=403)

    if _blocked(db, release_group_mbid=None, artist_mbid_=artist_mbid_):
        raise RequestError("This artist is blocked", code="blocked", status=403)

    plan = await plan_artist_request(db, artist_mbid_, scope=scope, mb=mb)
    if not plan["albums"]:
        raise RequestError(
            "Nothing to request: every album in this scope is already in your "
            "library or already requested", code="nothing_to_request", status=409)

    parent = Request(
        user_id=row.id,
        kind="artist",
        artist_mbid=plan["artist_mbid"],
        artist_name=plan["artist_name"],
        album_title=None,
        scope=scope,
        status="pending",
        note=note,
    )
    db.add(parent)
    db.flush()

    children = [
        Request(
            user_id=row.id,
            kind="album",
            release_group_mbid=album["release_group_mbid"],
            artist_mbid=plan["artist_mbid"],
            artist_name=plan["artist_name"],
            album_title=album["title"],
            year=album["year"],
            primary_type=album["primary_type"],
            status="pending",
            parent_request_id=parent.id,
        )
        for album in plan["albums"]
    ]
    db.add_all(children)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RequestError(
            "Another request for one of these albums landed at the same time — "
            "reload and try again", code="conflict", status=409)

    db.refresh(parent)
    add_vote(db, parent, user)

    if row.auto_approve:
        await approve(db, parent, user)

    db.refresh(parent)
    return {"request": parent, "created": True,
            "children": plan["summary"]["requestable"]}


def rollup(db: Session, request: Request) -> dict:
    """Status counts for an artist request, computed on read.

    Computed rather than stored: children change status on the reconciler's
    schedule, and a denormalised counter would drift the first time a child was
    retried outside the parent's flow.
    """
    if request.kind != "artist":
        return {}
    rows = (
        db.query(Request.status)
        .filter(Request.parent_request_id == request.id)
        .all()
    )
    counts: dict[str, int] = {}
    for (status,) in rows:
        counts[status] = counts.get(status, 0) + 1
    return {"requested": len(rows), **counts}
