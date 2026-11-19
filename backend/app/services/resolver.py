"""Local resolver: ListenBrainz entries -> Navidrome song IDs.

Two local indexes, built at sync time, both zero-API-call at query time:

1. `tracks.recording_mbid` — exact, but only present on tagged albums. Live
   measurement: 50.2% of the library, and tagging is per-album (59 all-tagged /
   61 untagged / 0 mixed out of 120 albums), so it is all-or-nothing per album.
2. `tracks.search_key` (normalized "<artist>|<title>") — covers the other half.
   ListenBrainz always supplies artist_name + track_name, so every listen has
   at least one viable resolution path.

MBID is tried first because it is exact. When it misses, the text index is tried
before giving up: ListenBrainz often has an MBID for a recording whose copy in
the library came from an untagged rip. That fallback is what converts a 50%
hit rate into a usable one.

Anything still unresolved comes back to the caller, which decides whether to
spend a Navidrome `search3` call on it (the scheduler caps those).
"""
import logging

from sqlalchemy.orm import Session

from app.models.database import Track
from app.services.sync import build_search_key

logger = logging.getLogger(__name__)


def resolve_mbids(db: Session, recording_mbids) -> dict[str, str]:
    """Map recording MBID -> Navidrome song id. One query, no API calls."""
    mbids = [m for m in dict.fromkeys(recording_mbids) if m]
    if not mbids:
        return {}

    rows = (
        db.query(Track.id, Track.recording_mbid)
        .filter(Track.recording_mbid.in_(mbids))
        .order_by(Track.id)
        .all()
    )
    resolved: dict[str, str] = {}
    for track_id, mbid in rows:
        if mbid and mbid not in resolved:
            resolved[mbid] = track_id
    return resolved


def resolve_text(db: Session, pairs) -> dict[str, str]:
    """Map normalized "<artist>|<title>" -> Navidrome song id. No API calls."""
    wanted = set()
    for artist, title in pairs:
        key = build_search_key(artist, title)
        if key != "|":
            wanted.add(key)
    if not wanted:
        return {}

    rows = (
        db.query(Track.id, Track.search_key)
        .filter(Track.search_key.in_(list(wanted)))
        .order_by(Track.id)
        .all()
    )
    resolved: dict[str, str] = {}
    ambiguous = 0
    for track_id, key in rows:
        if key in resolved:
            ambiguous += 1
            continue  # ordered by id, so the first (lowest) id wins
        resolved[key] = track_id

    if ambiguous:
        logger.info(
            "Resolver: %d duplicate search keys matched multiple tracks; "
            "kept the lowest track id for each", ambiguous,
        )
    return resolved


def resolve_entries(db: Session, entries: list[dict]) -> tuple[list[str], list[dict]]:
    """Resolve candidate entries to Navidrome song ids.

    Each entry needs `artist`, `title` and optionally `recording_mbid`.

    Returns `(resolved_song_ids, unresolved_entries)`:
    * resolved ids follow input order, de-duplicated
    * unresolved entries are passed back for optional `search3` fallback

    Resolution order per entry: MBID, then normalized text. An entry whose MBID
    is known to ListenBrainz but whose library copy is untagged still resolves
    through text — that is the common case for half this library.
    """
    if not entries:
        return [], []

    mbids = resolve_mbids(db, [e.get("recording_mbid") for e in entries])
    keys = resolve_text(db, [(e.get("artist"), e.get("title")) for e in entries])

    resolved: list[str] = []
    seen: set[str] = set()
    unresolved: list[dict] = []

    for entry in entries:
        song_id = None
        mbid = entry.get("recording_mbid")
        if mbid:
            song_id = mbids.get(mbid)
        if song_id is None:
            song_id = keys.get(build_search_key(entry.get("artist"), entry.get("title")))

        if song_id is None:
            unresolved.append(entry)
            continue
        if song_id not in seen:
            seen.add(song_id)
            resolved.append(song_id)

    logger.debug(
        "Resolver: %d entries -> %d resolved, %d unresolved",
        len(entries), len(resolved), len(unresolved),
    )
    return resolved, unresolved
