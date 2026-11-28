"""Resolve release-group MBIDs for library albums that lack them.

Runs as a **separate job**, not inside the library sync: it is a bulk one-time
copy plus a slow upstream tail, and nobody wants a sync to block on 400
MusicBrainz calls.

Measured on this library (3,626 albums), the tiers are wildly unequal in cost:

===================  =============  ==================
tier                 MusicBrainz    albums resolved
===================  =============  ==================
tags (Navidrome)     0              1,334
Lidarr oracle        0              1,210
MusicBrainz tail     ~400           ~200
===================  =============  ==================

So the oracle runs first and is the reason this feature is viable at all: most of
this library was *downloaded through Lidarr*, and Lidarr kept the release-group
id it resolved. Copying it is a one-time, auditable match that yields an exact
MBID rather than a runtime title guess.

Two rules come straight from measured failures and must not be "improved" into
strictness:

* **Title + artist establishes identity.** Year and track count only choose
  between multiple same-title candidates. Run DMC's *Raising Hell* carries
  ``maxYear`` 1999 against a 1986 release group (a reissue) and Notorious
  B.I.G.'s *Doing It Real Big* has 4 local tracks against 14 (a partial rip);
  both are correct matches that a strict gate rejects.
* **Ambiguity is skipped, not resolved.** A folded key mapping to more than one
  release group is the Greatest-Hits collision; those albums go to the tail
  instead of being guessed at.
"""
import logging
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.database import Album
from app.services.musicbrainz import (
    MusicBrainzClient,
    artist_credit_name,
    artist_mbid,
    first_release_year,
)
from app.services.title_norm import (
    album_key,
    loose_album_key,
    loose_title,
    norm_artist,
    norm_title,
    without_article,
)

logger = logging.getLogger(__name__)

# Secondary types that mean "this is not the studio album you are thinking of".
_JUNK_SECONDARY = frozenset({
    "remix", "demo", "bootleg", "interview", "mixtape/street", "dj-mix",
    "audiobook", "spokenword", "field recording",
})
_REQUESTABLE_PRIMARY = frozenset({"album", "ep"})

# Albums with a single track are usually rips of a single, where MusicBrainz
# frequently has no matching release group. Attempting them costs the most
# calls for the least reward; they are recorded as checked so they are not
# retried forever.
MIN_TRACKS = 2


def _local(album: Album) -> dict:
    return {
        "id": album.id,
        "name": album.name or "",
        "artist": album.artist_name or "",
        # Populated from Navidrome's `maxYear` by the tag pass; None until then.
        # Only ever a tiebreaker, never a veto.
        "year": album.year,
        "tracks": album.song_count or 0,
    }


def candidate_tier(local: dict, cand: dict) -> tuple[Optional[str], list[str]]:
    """Grade one candidate against one local album.

    Returns ``(tier, reasons)`` or ``(None, [])`` when the candidate is not the
    same album. ``tier`` is ``high`` (exact title + artist), ``medium`` (exact
    title, artist variant) or ``low`` (title matches only after dropping
    brackets — edition noise).
    """
    local_title = local.get("name") or ""
    local_artist = local.get("artist") or ""

    cand_title = cand.get("title") or ""
    cand_artist = artist_credit_name(cand)

    reasons: list[str] = []

    if norm_title(cand_title) == norm_title(local_title) and norm_title(local_title):
        title_tier = "exact"
    elif loose_title(cand_title) == loose_title(local_title) and loose_title(local_title):
        title_tier = "loose"
        reasons.append("title differs only by bracketed text")
    elif without_article(cand_title) == without_article(local_title) and without_article(local_title):
        title_tier = "loose"
        reasons.append("title differs by a leading article")
    else:
        return None, []

    if norm_artist(cand_artist) == norm_artist(local_artist):
        artist_tier = "exact"
    elif (
        norm_artist(local_artist)
        and norm_artist(cand_artist)
        and (norm_artist(local_artist) in norm_artist(cand_artist)
             or norm_artist(cand_artist) in norm_artist(local_artist))
        and min(len(norm_artist(local_artist)), len(norm_artist(cand_artist))) >= 5
    ):
        artist_tier = "loose"
        reasons.append("artist name variant")
    else:
        return None, []

    secondary = {s.lower() for s in cand.get("secondary-types") or []}
    if secondary & _JUNK_SECONDARY and not any(
        word in norm_title(local_title) for word in ("live", "remix", "demo")
    ):
        return None, []

    primary = (cand.get("primary-type") or "Album").lower()
    if primary not in _REQUESTABLE_PRIMARY:
        return None, []

    if title_tier == "exact" and artist_tier == "exact":
        tier = "high"
    elif title_tier == "exact":
        tier = "medium"
    else:
        tier = "low"

    return tier, reasons


def _year_gap(local: dict, cand: dict) -> Optional[int]:
    local_year, cand_year = local.get("year"), first_release_year(cand)
    if local_year and cand_year:
        return abs(int(local_year) - int(cand_year))
    return None


def pick_best(local: dict, candidates: Iterable[dict]) -> tuple[Optional[dict], Optional[str], int]:
    """Choose the best candidate and report how many distinct ones matched.

    Year is used only to order candidates, never to veto one: an album whose
    local year records a reissue still belongs to the original release group.
    """
    order = {"high": 0, "medium": 1, "low": 2}
    scored = []
    for cand in candidates:
        tier, reasons = candidate_tier(local, cand)
        if tier is None:
            continue
        gap = _year_gap(local, cand)
        scored.append((order[tier], gap if gap is not None else 99, cand, tier, reasons))

    if not scored:
        return None, None, 0

    scored.sort(key=lambda row: (row[0], row[1], -(row[2].get("score") or 0)))
    best = scored[0]
    distinct = len({row[2].get("id") for row in scored})
    return best[2], best[3], distinct


def untagged(db: Session, *, retry_failed: bool = False) -> list[Album]:
    """Albums still missing a release group, optionally including the floor.

    ``mbid_checked_at`` is the resumability marker: NULL means "never looked",
    so a re-run does not re-query 800 albums that genuinely have no match.
    """
    query = db.query(Album).filter(Album.release_group_mbid.is_(None))
    if not retry_failed:
        query = query.filter(Album.mbid_checked_at.is_(None))
    return query.all()


def _mark_checked(album: Album, *, source: Optional[str] = None,
                  confidence: Optional[str] = None,
                  mbid: Optional[str] = None) -> None:
    if mbid:
        album.release_group_mbid = mbid
        album.mbid_source = source
        album.mbid_confidence = confidence
    album.mbid_checked_at = datetime.utcnow()


# ── Tier 2: Lidarr as an oracle ─────────────────────────────────────────────


def oracle_tier(db: Session, lidarr_albums: Iterable[dict]) -> dict:
    """Copy release-group ids Lidarr already resolved.

    Zero upstream calls. Ids are copied only where the folded ``artist|title``
    key maps to exactly one release group on each side; anything ambiguous is
    left for the tail rather than guessed.
    """
    exact: dict[str, set[str]] = defaultdict(set)
    loose: dict[str, set[str]] = defaultdict(set)
    for row in lidarr_albums:
        mbid = row.get("foreignAlbumId")
        if not mbid:
            continue
        artist = ((row.get("artist") or {}).get("artistName")) or ""
        title = row.get("title") or ""
        exact[album_key(artist, title)].add(mbid)
        loose[loose_album_key(artist, title)].add(mbid)

    stats = {"considered": 0, "resolved": 0, "ambiguous": 0, "unmatched": 0}
    for album in untagged(db):
        stats["considered"] += 1
        key = album_key(album.artist_name or "", album.name or "")
        loose_key = loose_album_key(album.artist_name or "", album.name or "")

        if key in exact:
            ids = exact[key]
            if len(ids) == 1:
                _mark_checked(album, mbid=next(iter(ids)),
                              source="lidarr_oracle", confidence="high")
                stats["resolved"] += 1
            else:
                stats["ambiguous"] += 1
            continue

        if loose_key in loose:
            ids = loose[loose_key]
            if len(ids) == 1:
                _mark_checked(album, mbid=next(iter(ids)),
                              source="lidarr_oracle", confidence="medium")
                stats["resolved"] += 1
            else:
                stats["ambiguous"] += 1
            continue

        stats["unmatched"] += 1

    db.commit()
    logger.info("MBID oracle tier: %s", stats)
    return stats


# ── Tier 3: MusicBrainz tail ────────────────────────────────────────────────


async def _resolve_artist(mb: MusicBrainzClient, name: str) -> Optional[dict]:
    """Find the artist whose folded name matches exactly, else None.

    Deliberately does *not* take the first search result: MusicBrainz holds
    several artists named ENHYPEN, and the first match had three albums where
    the group has dozens.
    """
    wanted = norm_artist(name)
    for candidate in await mb.search_artists(name, limit=5):
        if norm_artist(candidate.get("name")) == wanted:
            return candidate
    return None


async def tail_tier(db: Session, mb: MusicBrainzClient, *,
                    artist_limit: Optional[int] = None,
                    min_tracks: int = MIN_TRACKS,
                    record_aliases: bool = True) -> dict:
    """Resolve the remainder by browsing each artist's discography once."""
    stats = {
        "artists": 0, "albums": 0, "resolved": 0, "medium": 0,
        "ambiguous": 0, "unmatched": 0, "artist_unresolved": 0,
        "skipped_singles": 0,
    }

    by_artist: dict[str, list[Album]] = defaultdict(list)
    for album in untagged(db):
        if (album.song_count or 0) < min_tracks:
            _mark_checked(album)
            stats["skipped_singles"] += 1
            continue
        by_artist[norm_artist(album.artist_name or "")].append(album)

    db.commit()

    keys = [key for key in by_artist if key]
    keys.sort()
    if artist_limit is not None:
        keys = keys[:artist_limit]

    for key in keys:
        albums = by_artist[key]
        stats["artists"] += 1
        artist = await _resolve_artist(mb, albums[0].artist_name or "")
        if artist is None:
            for album in albums:
                _mark_checked(album)
            stats["artist_unresolved"] += len(albums)
            db.commit()
            continue

        release_groups: list[dict] = []
        for primary in ("album", "ep"):
            # One `type` per call: repeating the parameter returns 503.
            release_groups.extend(
                await mb.browse_artist_release_groups(artist["id"], type_=primary)
            )

        for album in albums:
            stats["albums"] += 1
            local = _local(album)
            best, tier, distinct = pick_best(local, release_groups)

            if best is None:
                _mark_checked(album)
                stats["unmatched"] += 1
                continue
            if distinct > 1 and tier != "high":
                _mark_checked(album)
                stats["ambiguous"] += 1
                continue

            _mark_checked(album, mbid=best.get("id"),
                          source="musicbrainz_backfill", confidence=tier)
            if tier == "high":
                stats["resolved"] += 1
            else:
                stats["medium"] += 1

        db.commit()

    logger.info("MBID tail tier: %s", stats)
    return stats


# ── Orchestration ───────────────────────────────────────────────────────────


def report(db: Session) -> dict:
    """Coverage snapshot, suitable for logging or an endpoint payload."""
    total = db.query(Album).count()
    by_source: dict[str, int] = defaultdict(int)
    for album in db.query(Album).filter(Album.release_group_mbid.isnot(None)).all():
        by_source[album.mbid_source or "unknown"] += 1
    tagged = sum(by_source.values())
    return {
        "albums": total,
        "tagged": tagged,
        "coverage": round(tagged / total, 4) if total else 0.0,
        "by_source": dict(by_source),
        "never_checked": db.query(Album)
        .filter(Album.release_group_mbid.is_(None), Album.mbid_checked_at.is_(None))
        .count(),
        "checked_no_match": db.query(Album)
        .filter(Album.release_group_mbid.is_(None), Album.mbid_checked_at.isnot(None))
        .count(),
    }


async def run(db: Session, *, mb: MusicBrainzClient,
              lidarr_albums: Optional[Iterable[dict]] = None,
              include_tail: bool = True) -> dict:
    """Run the oracle tier, then optionally the tail, and report coverage."""
    before = report(db)
    result: dict = {"before": before}

    if lidarr_albums is not None:
        result["oracle"] = oracle_tier(db, lidarr_albums)

    if include_tail:
        result["tail"] = await tail_tier(db, mb)

    result["after"] = report(db)
    return result
