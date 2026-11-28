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
from app.services.title_norm import album_key

logger = logging.getLogger(__name__)

# Only the visible page gets artwork: CAA is one request per album, and a card
# the user cannot see is a request nobody needed.
COVER_LIMIT = 10

OPEN_STATUSES = ("pending", "approved", "processing")


# ── Ownership and requests ──────────────────────────────────────────────────


class OwnershipIndex:
    """Which library albums exist, by MBID and by folded artist|album."""

    def __init__(self, mbids: set[str], names: set[str]):
        self.mbids = mbids
        self.names = names

    def lookup(self, release_group_mbid: Optional[str], artist: str, title: str):
        if release_group_mbid and release_group_mbid in self.mbids:
            return True, "mbid"
        if album_key(artist or "", title or "") in self.names:
            return True, "name"
        return False, None


def ownership_index(db: Session) -> OwnershipIndex:
    rows = db.query(Album.release_group_mbid, Album.artist_name, Album.name).all()
    mbids = {row[0] for row in rows if row[0]}
    names = set()
    for _, artist, name in rows:
        if name:
            names.add(album_key(artist or "", name))
    return OwnershipIndex(mbids, names)


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
             covers: Optional[dict[str, Optional[str]]] = None) -> list[dict]:
    """Turn release-group payloads into cards with ownership and requests."""
    index = index or ownership_index(db)
    requests = _open_requests(db, [rg.get("id") for rg in release_groups])
    covers = covers or {}

    out = []
    for rg in release_groups:
        rgid = rg.get("id")
        owned, via = index.lookup(rgid, artist_credit_name(rg), rg.get("title") or "")
        out.append(card(rg, owned=owned, owned_via=via,
                        request=requests.get(rgid),
                        cover_url=covers.get(rgid)))
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

    client = mb or MusicBrainzClient()
    owns_client = mb is None
    try:
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

        cards = annotate(db, release_groups, covers=await _covers(db, release_groups))
        result = {
            "results": cards,
            "count": payload.get("count", len(cards)),
            "offset": offset,
            "artist_suggestions": [],
        }

        if not cards:
            # No release group matched. An artist is a useful next step and is
            # often what the user meant ("typing an artist name in the album box").
            artists = await client.search_artists(query, limit=5)
            result["artist_suggestions"] = [
                {"artist_mbid": a.get("id"), "artist_name": a.get("name"),
                 "disambiguation": a.get("disambiguation")}
                for a in artists if a.get("id")
            ]
        return result
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
    from app.services.lb_rails import artist_popularity, fresh_releases

    fresh = await fresh_releases(db, days=days)
    release_groups = [
        {"id": row["release_group_mbid"], "title": row.get("title"),
         "artist-credit": [{"name": row.get("artist_name") or "",
                            "artist": {"id": row.get("artist_mbid")}}],
         "primary-type": row.get("primary_type") or "Album",
         "first-release-date": row.get("year") and f"{row['year']}",
         "score": None}
        for row in fresh if row.get("release_group_mbid")
    ]

    cards = annotate(db, release_groups, covers=await _covers(db, release_groups))

    artist_ids = {c["artist_mbid"] for c in cards if c["artist_mbid"]}
    counts = await artist_popularity(sorted(artist_ids))
    trending = sorted(cards, key=lambda c: counts.get(c["artist_mbid"], 0), reverse=True)[:10]

    return {"new_releases": cards, "trending": trending}
