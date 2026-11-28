"""One catalog response shape, annotated for the caller.

Search, album detail, artist detail and the rails all return the same card, so
the client has a single renderer and the ownership logic lives in one place.

Ownership is deliberately two-tier. A **release-group MBID** match is exact and
says "you have this". A **folded name** match (``artist|album``) is a maybe, and
is labelled as such: 716 of this library's albums are single-track rips and 135
carry ``[Unknown Album]``, none of which any MBID backfill will ever reach. Name
matching needs no MBID, which turns that floor from a correctness hole into a
"probably in your library" tooltip — a user who already owns an untagged rip is
not shown a duplicate request button.

Requests are annotated from the ``requests`` table and never from upstream: a
card must not cost a MusicBrainz call just to say "requested".
"""
import asyncio
import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.database import Album, Request, User
from app.services import album_aliases
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
from app.services.requests import OPEN_STATUSES
from app.services.title_norm import album_key

logger = logging.getLogger(__name__)

# Only the visible page gets artwork: CAA is one request per album, and a card
# the user cannot see is a request nobody needed.
COVER_LIMIT = 10

# A rail is browsed, not paginated. The fresh-releases feed carries ~3,600 rows
# for a fortnight; rendering them all cost 37 seconds and a wall of DOM nodes,
# and spent cover lookups on cards nobody would ever scroll to.
RAIL_LIMIT = 24
TRENDING_LIMIT = 12

# Bump when the cached search payload changes shape or gains a field.
SEARCH_CACHE_VERSION = "v2"
# Popularity is one POST per 25 ids. The pool is wider than the rail so
# "trending" ranks across a fortnight of releases rather than only the two dozen
# newest — brand-new albums have barely any listens yet, so a pool the size of
# the rail usually yields a one-item trending list.
RAIL_POOL = 200

# A name match is allowed to be one track off. Different editions of the same
# album routinely differ by a bonus track, and refusing those would report a
# genuinely owned album as missing.
TRACK_COUNT_TOLERANCE = 1

# Every status in which a release group is still "spoken for". The definition
# lives with the status machine in services.requests, so a card badge, the
# request list and the partial unique index cannot disagree about whether a
# second ask is a duplicate.


# ── Ownership and requests ──────────────────────────────────────────────────


class OwnershipIndex:
    """Which library albums exist, by MBID and by folded artist|album.

    The name path optionally requires track-count agreement. Titles repeat
    across editions and artists release live/remaster versions of the same
    album, so `artist|title` alone reports "owned" for things that are not
    there; a track count within one of what MusicBrainz says turns that into a
    real match. Cards pass no count (they would need an extra MB call per
    result) and so keep the looser behaviour; the reconciler always passes the
    count Lidarr reports, because there a false "available" is a lie to the
    person waiting on the request.
    """

    def __init__(self, rows: Iterable[tuple]):
        # rows: (release_group_mbid, artist_name, album_name, song_count)
        self.mbids: set[str] = set()
        self.names: dict[str, list[int]] = {}
        for mbid, artist, name, song_count in rows:
            if mbid:
                self.mbids.add(mbid)
            if name:
                key = album_key(artist or "", name)
                self.names.setdefault(key, []).append(song_count or 0)

    def lookup(self, release_group_mbid: Optional[str], artist: str, title: str,
               song_count: Optional[int] = None):
        """Return ``(owned, evidence)`` where evidence is ``"mbid"``/``"name"``."""
        if release_group_mbid and release_group_mbid in self.mbids:
            return True, "mbid"
        if not title:
            return False, None
        counts = self.names.get(album_key(artist or "", title))
        if counts is None:
            return False, None
        if song_count is None:
            return True, "name"
        if any(abs(have - song_count) <= TRACK_COUNT_TOLERANCE for have in counts):
            return True, "name"
        return False, None


def ownership_index(db: Session) -> OwnershipIndex:
    rows = db.query(
        Album.release_group_mbid, Album.artist_name, Album.name, Album.song_count
    ).all()
    return OwnershipIndex(rows)


def library_has_release_group(
    db: Session,
    release_group_mbid: Optional[str],
    artist: str,
    title: str,
    song_count: Optional[int] = None,
    *,
    index: Optional[OwnershipIndex] = None,
) -> Optional[str]:
    """The single availability predicate: ``"mbid"``, ``"name"`` or ``None``.

    Both the catalog cards and the request reconciler come through here, so the
    ``In library`` badge in search and the ``available`` status on a request
    cannot be computed two different ways.
    """
    index = index or ownership_index(db)
    owned, evidence = index.lookup(release_group_mbid, artist, title, song_count)
    return evidence if owned else None


def _open_requests(db: Session, mbids: Iterable[str]) -> dict[str, dict]:
    wanted = {m for m in mbids if m}
    if not wanted:
        return {}
    rows = (
        db.query(Request, User)
        .outerjoin(User, User.id == Request.user_id)
        .filter(Request.release_group_mbid.in_(wanted),
                Request.kind == "album",
                Request.status.in_(OPEN_STATUSES))
        .all()
    )
    out: dict[str, dict] = {}
    for request, user in rows:
        # A denied or cancelled request never blocks a new card, so only open
        # ones land here; the filter above already guarantees that.
        out.setdefault(request.release_group_mbid, {
            "id": request.id,
            "status": request.status,
            "requested_by": (user.username if user else None),
        })
    return out


# ── Card construction ───────────────────────────────────────────────────────


def card(rg: dict, *, owned: bool = False, owned_via: Optional[str] = None,
         request: Optional[dict] = None,
         cover_url: Optional[str] = None) -> dict:
    return {
        "release_group_mbid": rg.get("id"),
        "title": rg.get("title"),
        "artist_name": artist_credit_name(rg),
        "artist_mbid": artist_mbid(rg),
        "year": first_release_year(rg),
        "primary_type": rg.get("primary-type") or "Album",
        "secondary_types": list(rg.get("secondary-types") or []),
        "cover_url": cover_url,
        "score": rg.get("score"),
        "owned": owned,
        "owned_via": owned_via,
        "request": request,
    }


def annotate(db: Session, release_groups: list[dict], *,
             index: Optional[OwnershipIndex] = None,
             covers: Optional[dict[str, Optional[str]]] = None,
             blocked: Optional[tuple[set[str], set[str]]] = None) -> list[dict]:
    """Turn release-group payloads into cards with ownership and requests.

    ``blocked`` is the ``(release_groups, artists)`` pair from
    ``services.blocklist.blocked_sets``. Cards carry a ``blocked`` flag rather
    than being dropped here: the caller decides, because search hides blocked
    results while an artist page shows them greyed out — an album silently
    missing from a discography reads as a bug.
    """
    index = index or ownership_index(db)
    requests = _open_requests(db, [rg.get("id") for rg in release_groups])
    covers = covers or {}
    if blocked is None:
        from app.services.blocklist import blocked_sets

        blocked = blocked_sets(db)
    blocked_rgs, blocked_artists = blocked

    out = []
    for rg in release_groups:
        rgid = rg.get("id")
        owned, via = index.lookup(rgid, artist_credit_name(rg), rg.get("title") or "")
        entry = card(rg, owned=owned, owned_via=via,
                     request=requests.get(rgid),
                     cover_url=covers.get(rgid))
        entry["blocked"] = bool(
            (rgid and rgid in blocked_rgs)
            or (entry.get("artist_mbid") and entry["artist_mbid"] in blocked_artists)
        )
        out.append(entry)
    return out


async def _covers(db: Session, release_groups: list[dict]) -> dict[str, Optional[str]]:
    ids = [rg.get("id") for rg in release_groups if rg.get("id")][:COVER_LIMIT]
    if not ids:
        return {}
    try:
        return await front_covers(db, ids)
    except Exception:  # artwork must never break a search
        logger.warning("cover art fetch failed", exc_info=True)
        return {}


# ── Search ──────────────────────────────────────────────────────────────────


async def search(db: Session, user: dict, query: str, *, limit: int = 10,
                 offset: int = 0, mb: Optional[MusicBrainzClient] = None) -> dict:
    """Search MusicBrainz, seeded by any local alias hit.

    A localised title ("PSY 9th" for ``싸다9``) is matched against harvested
    aliases first, so a famously mis-titled album surfaces without spending a
    MusicBrainz query on a string MusicBrainz does not use as its title.
    """
    query = (query or "").strip()
    if not query:
        return {"results": [], "count": 0, "offset": offset, "artist_suggestions": []}

    # Only upstream data is cached. Ownership and request badges are recomputed
    # on every call: caching those would keep offering "Request" on an album
    # somebody asked for a minute ago, which reads as the request having failed.
    #
    # The version is part of the key on purpose. When artist suggestions became
    # unconditional, every query cached under the old behaviour kept serving an
    # empty suggestion list for its full ten minutes — the change was correct and
    # invisible. Bumping this retires the old payloads instead of waiting them out.
    key = make_key("search", SEARCH_CACHE_VERSION, query, limit, offset)
    cached = cache_get(db, key, "search")

    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        if cached is not None:
            release_groups = cached.get("release_groups") or []
            covers = cached.get("covers") or {}
            total = cached.get("count") or len(release_groups)
            suggestions = cached.get("artist_suggestions") or []
        else:
            payload = await client.search_release_groups(query, limit=limit, offset=offset)
            release_groups = payload.get("release_groups", [])

            alias_hits = album_aliases.match(db, query, limit=limit)
            if alias_hits:
                seen = {rg.get("id") for rg in release_groups}
                missing = [m for m in alias_hits if m not in seen]
                if missing:
                    fetched = await asyncio.gather(
                        *(client.get_release_group(m, inc="artist-credits") for m in missing),
                        return_exceptions=True,
                    )
                    for detail in fetched:
                        if isinstance(detail, dict):
                            release_groups.insert(0, detail)

            covers = await _covers(db, release_groups)

            # Always offered, not only when nothing matched. Searching an artist
            # name returns albums that merely share the word — "twice" surfaces
            # three albums called *Twice* by other artists, and without an artist
            # strip there is no way to reach TWICE from here.
            artists = await client.search_artists(query, limit=3)
            suggestions = [
                {"artist_mbid": a.get("id"), "artist_name": a.get("name"),
                 "disambiguation": a.get("disambiguation")}
                for a in artists if a.get("id")
            ]
            total = payload.get("count", len(release_groups))
            cache_put(db, key, "search", {
                "release_groups": release_groups,
                "covers": covers,
                "count": total,
                "artist_suggestions": suggestions,
            })

        return {
            # Blocked albums are dropped from search: offering something the
            # household has rejected is noise, and the request would be refused
            # anyway. Artist pages keep them (flagged) — see artist_detail.
            "results": [c for c in annotate(db, release_groups, covers=covers)
                        if not c.get("blocked")],
            "count": total,
            "offset": offset,
            "artist_suggestions": suggestions,
        }
    finally:
        if owns_client:
            await client.close()


# ── Album detail ────────────────────────────────────────────────────────────


async def album_detail(db: Session, user: dict, release_group_mbid: str, *,
                       mb: Optional[MusicBrainzClient] = None) -> Optional[dict]:
    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        detail = await client.get_release_group(
            release_group_mbid, inc="artist-credits+releases")
        if not detail:
            return None

        # First sight of this release group is the cheap moment to learn its
        # localised titles, which make a later search resolve locally.
        album_aliases.record_aliases(db, release_group_mbid, detail)

        covers = await _covers(db, [detail])
        annotated = annotate(db, [detail], covers=covers)[0]
        annotated["aliases"] = album_aliases.aliases_for(db, release_group_mbid)
        annotated["requestable"] = is_requestable(detail)
        annotated["releases"] = [
            {"id": r.get("id"), "title": r.get("title"), "date": r.get("date"),
             "country": r.get("country")}
            for r in detail.get("releases") or []
        ]
        return annotated
    finally:
        if owns_client:
            await client.close()


# ── Artist detail ───────────────────────────────────────────────────────────


async def artist_detail(db: Session, user: dict, artist_mbid_: str, *,
                        mb: Optional[MusicBrainzClient] = None) -> Optional[dict]:
    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
        artist = await client.get_artist(artist_mbid_)
        if not artist:
            return None

        release_groups: list[dict] = []
        for primary in ("album", "ep"):
            release_groups.extend(
                await client.browse_artist_release_groups(artist_mbid_, type_=primary))

        cards = annotate(db, release_groups, covers=await _covers(db, release_groups))
        owned = sum(1 for c in cards if c["owned"])
        return {
            "artist_mbid": artist.get("id"),
            "artist_name": artist.get("name"),
            "disambiguation": artist.get("disambiguation"),
            "albums": cards,
            "summary": {
                "albums": len(cards),
                "owned": owned,
                "requestable": sum(1 for c in cards if not c["owned"] and not c["request"]),
            },
        }
    finally:
        if owns_client:
            await client.close()


# ── Rails ───────────────────────────────────────────────────────────────────


async def rails(db: Session, user: dict, *, mb: Optional[MusicBrainzClient] = None,
                days: int = 14) -> dict:
    """New releases, plus the most-listened among them as "trending".

    Trending is derived from the new-release set rather than the whole catalog:
    it is one batched ListenBrainz call over ids we already hold, and it keeps
    the two rails visually related instead of unrelated noise.
    """
    from app.services.lb_rails import fresh_releases, popularity

    fresh = await fresh_releases(db, days=days)

    # Newest first, then slice. The feed is roughly date-ordered already and the
    # explicit sort costs nothing next to being wrong about "new".
    rows = sorted(
        (r for r in fresh if r.get("release_group_mbid")),
        key=lambda r: (r.get("release_date") or "", r.get("year") or 0),
        reverse=True,
    )[:RAIL_POOL]

    release_groups = [
        {"id": row["release_group_mbid"], "title": row.get("title"),
         "artist-credit": [{"name": row.get("artist_name") or "",
                            "artist": {"id": row.get("artist_mbid")}}],
         "primary-type": row.get("primary_type") or "Album",
         "first-release-date": row.get("year") and f"{row['year']}",
         "score": None}
        for row in rows
    ]

    # ListenBrainz already hands us a Cover Art Archive release id for most rows,
    # so most artwork is free. Only the gaps cost a MusicBrainz lookup, which is
    # why this is no longer thousands of requests for a page that shows two dozen.
    lb_covers = {row["release_group_mbid"]: row.get("cover_url") for row in rows}
    covers = {rid: url for rid, url in lb_covers.items() if url}
    gaps = [rg for rg in release_groups if rg["id"] not in covers]
    if gaps:
        covers.update(await _covers(db, gaps))

    pool = annotate(db, release_groups, covers=covers)
    # Blocked artists and albums are hidden from rails for the same reason they
    # are hidden from search: the household has said no.
    pool = [c for c in pool if not c.get("blocked")]
    cards = pool[:RAIL_LIMIT]

    # Trending means "the most-listened among these new releases", so popularity
    # is asked about the release groups themselves. Ranking by *artist* popularity
    # instead would surface whichever newcomer happens to have the largest
    # back catalogue following — Queen, in practice — which is not what the rail
    # claims to be.
    counts = await popularity([c["release_group_mbid"] for c in pool])
    trending = sorted(
        pool, key=lambda c: counts.get(c["release_group_mbid"], 0), reverse=True
    )[:TRENDING_LIMIT]

    return {
        "new_releases": cards,
        "trending": [c for c in trending if counts.get(c["release_group_mbid"], 0) > 0],
    }
