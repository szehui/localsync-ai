"""Smart Triggers scheduler service.

Manages APScheduler jobs for each enabled trigger in the database.
When triggers are created, updated, deleted, or toggled, the corresponding
scheduler job is added, modified, removed, or paused.
"""
import json
import logging
import random
import re
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from app.models.database import SmartTrigger, GeneratedPlaylist, ConnectionConfig, SessionLocal, Track, LastfmConfig, ListenbrainzConfig
from app.services.navidrome import NavidromeClient
from app.services.lastfm import LastfmClient
from app.services.listenbrainz import ListenBrainzClient
from app.services.lb_cache import (
    get_cached_recommendations,
    get_cached_similar,
    put_recommendations,
    put_similar,
)
from app.services.resolver import resolve_entries
from app.services.curation import (
    MIN_PLAYLIST_TRACKS,
    aging_penalty,
    diversify,
    normalize_scores,
    seeded_rng,
)
from app.services.mixes import DEFAULT_MIX, MIX_SPECS, select_mix
from app.routers.playlists import _passes_strictness

logger = logging.getLogger(__name__)

# ListenBrainz tuning. The API budget is 30 requests/window at ~1 req/sec, so
# seeds are capped and every response is cached before it is reused.
LB_SEED_LIMIT = 20            # Labs similar-recordings calls per run
LB_SEARCH_FALLBACK_CAP = 10   # Navidrome search3 calls for unresolved entries
MIN_LB_LISTENS = 10           # below this, LB history is too thin to trust alone
LB_RECENCY_DAYS = 7           # listening-history window
LB_CANDIDATE_TARGET = 30      # enough history candidates to fill the 60% side

# Reference to the global scheduler instance (set from main.py)
_scheduler = None

# Dated playlists (e.g. 'Fresh Discoveries YYYY-MM-DD') pile up one per day, so
# the recency trigger prunes ones past this window once it writes a new one.
PLAYLIST_RETENTION_DAYS = 14


def init_scheduler(scheduler):
    """Called from main.py to give us a reference to the running scheduler."""
    global _scheduler
    _scheduler = scheduler


def _get_scheduler():
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized")
    return _scheduler


def _job_id(trigger: SmartTrigger) -> str:
    return f"trigger-{trigger.id}"


# ─── Cooldown / Recency Exclusion ───────────────────────────────────────────

def _get_cooldown_track_ids(db: Session, name_prefix: str, days: int = 2) -> set:
    """Return the union of track_ids from GeneratedPlaylist rows whose name
    starts with `name_prefix` and which were updated within the last `days` days.

    Used to exclude songs that have already appeared in recent runs of a
    same-named playlist (e.g. the daily 'Fresh Discoveries YYYY-MM-DD' list),
    so the recency trigger doesn't pick the same songs day after day.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(GeneratedPlaylist)
        .filter(GeneratedPlaylist.updated_at >= cutoff)
        .filter(GeneratedPlaylist.name.like(f"{name_prefix}%"))
        .all()
    )
    blocked: set = set()
    for row in rows:
        # `track_ids` is a JSON-encoded list of strings; type annotation is Column[str]
        # but at runtime it is a plain str | None.
        raw = row.track_ids  # type: ignore[assignment]
        if not raw:
            continue
        try:
            ids = json.loads(str(raw))
        except (ValueError, TypeError):
            # Malformed JSON in an old row — skip rather than crash the trigger.
            logger.warning(f"Cooldown: skipping row {row.id} ({row.name}) with malformed track_ids")
            continue
        if isinstance(ids, list):
            blocked.update(ids)
    return blocked


def _cooldown_prefix_for(playlist_name: str) -> str:
    """Derive the cooldown lookup prefix from a trigger's playlist name.

    Default recency playlists are named 'Fresh Discoveries YYYY-MM-DD' — one
    per day. To match prior days' playlists we strip the date suffix and use
    the base name as the prefix. Custom static names have no date to strip
    and are returned as-is (they cooldown against themselves, which is the
    correct semantics since the trigger overwrites the same playlist each run).
    """
    m = re.match(r'^(.*?)\s*\d{4}-\d{2}-\d{2}$', playlist_name)
    return m.group(1).strip() if m else playlist_name


# ─── Retention / pruning ────────────────────────────────────────────────────

def _playlist_date(playlist_name: str | None) -> datetime | None:
    """Parse the YYYY-MM-DD suffix of a dated playlist name.

    Returns None when the name carries no parseable date (custom static names,
    or a malformed suffix). Callers treat that as "not eligible for pruning".
    """
    m = re.search(r'(\d{4}-\d{2}-\d{2})$', (playlist_name or "").strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def _stale_playlist_rows(db: Session, reference_name: str, retention_days: int) -> list:
    """Return GeneratedPlaylist rows for the same dated series that are too old.

    Age comes from the date in the playlist name, not `updated_at` — the
    recency trigger rewrites today's playlist, so `updated_at` says nothing
    about how old the *list* is. Only names in the same series (sharing the
    reference name's prefix, date suffix stripped) are considered, so custom
    and undated playlists are never touched.
    """
    reference_date = _playlist_date(reference_name)
    if reference_date is None:
        return []

    prefix = _cooldown_prefix_for(reference_name)
    if not prefix:
        # Degenerate name (bare date) — an empty prefix would match every
        # playlist, so refuse to prune rather than wipe the library.
        return []
    cutoff = reference_date - timedelta(days=retention_days)

    rows = (
        db.query(GeneratedPlaylist)
        .filter(GeneratedPlaylist.name.like(f"{prefix}%"))
        .all()
    )
    stale = []
    for row in rows:
        name = (row.name or "").strip()
        if not name.startswith(prefix):
            continue  # LIKE is case-insensitive-ish in SQLite; be strict here
        date = _playlist_date(name)
        if date is None or date >= cutoff:
            continue
        stale.append(row)
    return stale


def _is_stale_series_name(name: str | None, prefix: str, cutoff: datetime) -> bool:
    """True when `name` belongs to the dated series and predates `cutoff`."""
    name = (name or "").strip()
    if not name.startswith(prefix):
        return False
    date = _playlist_date(name)
    return date is not None and date < cutoff


async def _prune_old_dated_playlists(
    db: Session,
    client: NavidromeClient,
    reference_name: str,
    retention_days: int = PLAYLIST_RETENTION_DAYS,
) -> int:
    """Delete playlists in the same dated series older than `retention_days`.

    Navidrome's own playlist list is the source of truth, matched **by name**:
    stored `navidrome_playlist_id` values drift over time (a Navidrome metadata
    reset reissues IDs), so trusting them silently orphans playlists that are
    still live — the row gets dropped while the playlist stays in Navidrome.
    Name is stable; the id is a hint at best.

    A DB row is only dropped once its playlist is confirmed gone (deleted now,
    or absent from the listing). Deletes are attempted first so a failed API
    call keeps the row for the next run to retry. Pruning must never take down
    a run that already produced a playlist, so every failure is logged and
    skipped rather than raised.

    Returns the number of Navidrome playlists removed.
    """
    reference_date = _playlist_date(reference_name)
    if reference_date is None:
        return 0
    prefix = _cooldown_prefix_for(reference_name)
    if not prefix:
        # Degenerate name (bare date) — an empty prefix matches every playlist.
        return 0
    cutoff = reference_date - timedelta(days=retention_days)

    stale_rows = _stale_playlist_rows(db, reference_name, retention_days)

    remote: list[dict] | None
    try:
        remote = list(await client.get_playlists() or [])
    except Exception as e:
        logger.warning(
            f"Prune: could not list Navidrome playlists ({e}) — "
            f"falling back to stored ids"
        )
        remote = None

    deleted_ids: set[str] = set()
    failed_ids: set[str] = set()
    pruned = 0

    if remote is not None:
        # Anything in Navidrome named '<prefix> YYYY-MM-DD' past the window goes,
        # whether or not the DB still has a row for it.
        victims = [
            p for p in remote
            if p.get("id") and _is_stale_series_name(p.get("name"), prefix, cutoff)
        ]
        for p in victims:
            pid = str(p["id"])
            name = (p.get("name") or "").strip()
            try:
                await client.delete_playlist(pid)
            except Exception as e:
                logger.warning(
                    f"Prune: could not delete Navidrome playlist '{name}' "
                    f"({pid}): {e} — keeping row for retry"
                )
                failed_ids.add(pid)
                continue
            deleted_ids.add(pid)
            pruned += 1
            logger.info(f"Pruned stale playlist '{name}' (older than {retention_days} days)")
    else:
        # No listing available: delete via stored ids and let name-matching on
        # the next successful run clean up anything the ids missed.
        for row in stale_rows:
            pid = row.navidrome_playlist_id
            if not pid or str(pid) in failed_ids:
                continue
            try:
                await client.delete_playlist(str(pid))
            except Exception as e:
                logger.warning(
                    f"Prune: could not delete Navidrome playlist '{row.name}' "
                    f"({pid}): {e} — keeping row for retry"
                )
                failed_ids.add(str(pid))
                continue
            deleted_ids.add(str(pid))
            pruned += 1
            logger.info(
                f"Pruned stale playlist '{row.name}' (older than {retention_days} days)"
            )

    # Drop stale DB rows, plus any row pointing at a playlist we just removed.
    # A row is kept when its playlist is still live in Navidrome (delete failed).
    live_names = {
        (p.get("name") or "").strip()
        for p in (remote or [])
        if p.get("id") and str(p["id"]) not in deleted_ids
    }
    seen: set[int] = set()
    for row in stale_rows:
        if id(row) in seen:
            continue
        seen.add(id(row))
        if remote is not None and (row.name or "").strip() in live_names:
            continue
        db.delete(row)

    return pruned


# ─── Playlist blending ──────────────────────────────────────────────────────

def _dedupe(ids) -> list[str]:
    """Preserve order, drop blanks and repeats."""
    seen = set()
    out = []
    for sid in ids:
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _blend_tracks(
    lb_similar,
    nav_similar,
    lfm_similar,
    newest_ids,
    target: int = 50,
    newest_share: float = 0.4,
    lb_share_of_history: float = 0.6,
    rng=None,
) -> list[str]:
    """Blend similarity pools and recently-added tracks into a playlist.

    Splits the target into a history side and a newest side (60/40 by default).
    On the history side ListenBrainz is primary: it takes `lb_share_of_history`
    of those slots and Navidrome similarity takes the rest. Last.fm is demoted
    to a fill source — it only contributes when ListenBrainz cannot cover its
    share, which is what keeps the app working on a Last.fm-free account.

    Shortfalls on either side are filled from whatever is left before the
    playlist is allowed to come out short, and the result is de-duplicated
    across all pools. Pure function: no DB, no network, seeded `rng` for tests.
    """
    rng = rng or random

    target = max(0, target)
    newest_target = int(newest_share * target)
    history_target = target - newest_target

    lb_pool = _dedupe(lb_similar)
    nav_pool = _dedupe(nav_similar)
    lfm_pool = _dedupe(lfm_similar)
    newest_pool = _dedupe(newest_ids)

    for pool in (lb_pool, nav_pool, lfm_pool, newest_pool):
        rng.shuffle(pool)

    lb_target = int(lb_share_of_history * history_target)
    nav_target = history_target - lb_target

    picked: list[str] = []
    seen: set[str] = set()

    # Pass 1 — honour each pool's share, in priority order.
    for pool, cap in ((lb_pool, lb_target), (nav_pool, nav_target)):
        taken = 0
        for sid in pool:
            if taken >= cap or len(picked) >= history_target:
                break
            if sid in seen:
                continue
            picked.append(sid)
            seen.add(sid)
            taken += 1

    # Pass 2 — cover a history shortfall: Last.fm first, then leftovers.
    for pool in (lfm_pool, lb_pool, nav_pool):
        for sid in pool:
            if len(picked) >= history_target:
                break
            if sid in seen:
                continue
            picked.append(sid)
            seen.add(sid)

    chosen_newest: list[str] = []
    for sid in newest_pool:
        if len(chosen_newest) >= newest_target:
            break
        if sid in seen:
            continue
        chosen_newest.append(sid)
        seen.add(sid)

    # Pass 3 — a thin newest side is topped up from history rather than
    # shipping a short playlist.
    if len(chosen_newest) < newest_target:
        gap = newest_target - len(chosen_newest)
        for pool in (lb_pool, nav_pool, lfm_pool):
            for sid in pool:
                if gap <= 0:
                    break
                if sid in seen:
                    continue
                picked.append(sid)
                seen.add(sid)
                gap -= 1

    final = picked + chosen_newest
    if len(final) < target:
        # Pass 4 — history could not cover its side (thin/absent listening
        # history, e.g. a fresh ListenBrainz account). Top up from the newest
        # leftovers so the playlist is full rather than mysteriously short.
        for sid in newest_pool:
            if len(final) >= target:
                break
            if sid in seen:
                continue
            final.append(sid)
            seen.add(sid)

    rng.shuffle(final)
    return final[:target]


# ─── Curation: diversity, aging, publish ────────────────────────────────────

def _track_meta(db: Session, track_ids) -> dict:
    """Map track id -> (artist, genre) with one query.

    Only what the diversity pass needs. Missing ids simply do not appear, which
    `_curate` treats as "unknown artist, no genre" rather than an error.
    """
    ids = [sid for sid in dict.fromkeys(track_ids) if sid]
    if not ids:
        return {}
    meta: dict = {}
    # SQLite caps bound parameters, so batch the IN clause.
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        rows = (
            db.query(Track.id, Track.artist_name, Track.genre)
            .filter(Track.id.in_(batch))
            .all()
        )
        for track_id, artist, genre in rows:
            meta[track_id] = ((artist or "").strip(), (genre or "").strip())
    return meta


def _aging_penalty(db: Session, track_ids, reference: datetime, lookback_days: int = 30) -> dict:
    """Map track id -> score penalty based on how recently it was offered.

    There is no "last offered" column; `GeneratedPlaylist.track_ids` already
    stores each run's track list as JSON, so the most recent appearance can be
    derived without a schema change. The penalty is linear in age up to a cap,
    which is what stops the same handful of Deep Cuts recurring every week —
    the 2-day cooldown alone is far too short at a weekly cadence.
    """
    ids = set(sid for sid in track_ids if sid)
    if not ids:
        return {}
    cutoff = reference - timedelta(days=lookback_days)
    rows = (
        db.query(GeneratedPlaylist.updated_at, GeneratedPlaylist.track_ids)
        .filter(GeneratedPlaylist.updated_at >= cutoff)
        .all()
    )
    last_seen: dict = {}
    for updated_at, raw in rows:
        if not raw or updated_at is None:
            continue
        try:
            stored = json.loads(str(raw))
        except (ValueError, TypeError):
            continue
        if not isinstance(stored, list):
            continue
        for sid in stored:
            if sid in ids and (sid not in last_seen or updated_at > last_seen[sid]):
                last_seen[sid] = updated_at

    penalties = {}
    for sid, seen_at in last_seen.items():
        days = (reference - seen_at).total_seconds() / 86400.0
        penalties[sid] = aging_penalty(days)
    return penalties


def _curate(db: Session, track_ids, target: int, rng, artist_cap: int = 5) -> list[str]:
    """Re-rank a blended track list for diversity, then trim to `target`.

    The blend happens on bare ids, so one artist can own half the playlist (a
    single well-connected similarity seed floods the pool). This applies a
    per-artist cap and then a greedy pass that penalises repeating an artist or
    a genre, with a decaying penalty for tracks served in recent runs.

    The cap is applied only when it leaves enough tracks to fill `target` —
    dropping 30 tracks to enforce variety would just ship a short playlist, so
    a pool dominated by three artists keeps its repeats and lets the penalty
    decide the running order instead.
    """
    ids = [sid for sid in dict.fromkeys(track_ids) if sid]
    if not ids:
        return []

    try:
        meta = _track_meta(db, ids)
    except Exception as e:
        # Curation is an enhancement, not a requirement: a missing/stale index
        # must never take down a trigger run that otherwise produced a playlist.
        logger.warning(f"Curation skipped, could not read track metadata: {e}")
        meta = {}
    if not meta:
        # Nothing in the local index (e.g. a fresh install) — no basis for
        # curation, so don't reorder what the blend produced.
        return ids[:target]

    penalized = _aging_penalty(db, ids, datetime.utcnow())

    capped: list[str] = []
    seen_artist: dict = {}
    dropped = 0
    for sid in ids:
        artist = meta.get(sid, ("", ""))[0]
        if artist and artist_cap > 0:
            if seen_artist.get(artist, 0) >= artist_cap:
                dropped += 1
                continue
            seen_artist[artist] = seen_artist.get(artist, 0) + 1
        capped.append(sid)

    if len(capped) < target:
        # Capping starved the playlist — keep every candidate and let the
        # diversity penalty handle the ordering instead.
        capped = ids
        dropped = 0

    # Blend order is the base score: earlier picks came from the higher
    # priority pools, so they should still win ties after curation. Normalise
    # *before* subtracting the aging penalty so the penalty is expressed on the
    # same 0..BASE_SCORE_SPAN scale as the diversity penalties — otherwise a
    # 3-point penalty against pre-normalisation scores only shifts a track a
    # couple of ranks and yesterday's playlist leads again.
    candidates = normalize_scores([
        {
            "id": sid,
            "artist": meta.get(sid, ("", ""))[0],
            "genre": meta.get(sid, ("", ""))[1],
            "score": float(len(capped) - i),
        }
        for i, sid in enumerate(capped)
    ])
    for candidate in candidates:
        candidate["score"] -= penalized.get(candidate["id"], 0.0)
    curated = diversify(candidates, target, rng=rng)
    if dropped:
        logger.info(
            f"Curation: capped {dropped} track(s) past {artist_cap}/artist; "
            f"{len(ids)} -> {len(curated)}"
        )
    return curated


def _publish_gate_ok(track_ids, name: str) -> bool:
    """Refuse to publish a stub playlist.

    A 6-track "Daily Mix" is worse than yesterday's playlist still being there,
    so short runs are skipped and the previous one is left alone.
    """
    if len(track_ids) >= MIN_PLAYLIST_TRACKS:
        return True
    logger.warning(
        f"'{name}': only {len(track_ids)} track(s), below the {MIN_PLAYLIST_TRACKS} "
        f"minimum — leaving the existing playlist untouched"
    )
    return False


async def _publish_playlist(
    db: Session,
    client: NavidromeClient,
    name: str,
    track_ids: list[str],
    prune: bool = False,
) -> bool:
    """Create or replace `name` in Navidrome and record it locally.

    Shared by every trigger so the delete-then-create dance (updatePlaylist
    appends rather than replaces) exists in exactly one place. Returns False
    when the publish gate rejected the run.
    """
    if not _publish_gate_ok(track_ids, name):
        return False

    existing = db.query(GeneratedPlaylist).filter(GeneratedPlaylist.name == name).first()
    if existing and existing.navidrome_playlist_id:
        try:
            await client.delete_playlist(existing.navidrome_playlist_id)
        except Exception as e:
            logger.warning(f"Failed to delete old playlist '{name}': {e}")
        result = await client.create_playlist(name, track_ids)
        existing.navidrome_playlist_id = result.get("playlist", {}).get("id", "")
        existing.track_ids = json.dumps(track_ids)
        existing.track_count = len(track_ids)
        existing.updated_at = datetime.utcnow()
    else:
        result = await client.create_playlist(name, track_ids)
        db.add(GeneratedPlaylist(
            name=name,
            navidrome_playlist_id=result.get("playlist", {}).get("id", ""),
            track_count=len(track_ids),
            track_ids=json.dumps(track_ids),
        ))
    db.commit()

    if prune:
        try:
            pruned = await _prune_old_dated_playlists(db, client, name)
            if pruned:
                logger.info(
                    f"Pruned {pruned} playlist(s) older than {PLAYLIST_RETENTION_DAYS} days"
                )
        except Exception as e:
            logger.warning(f"Playlist pruning failed (new playlist was created): {e}")

    return True


# ─── DB-backed Navidrome Client ─────────────────────────────────────────────

def _get_client_from_db() -> NavidromeClient:
    """Create a NavidromeClient using credentials stored in the ConnectionConfig table.

    Raises RuntimeError if no saved config exists.
    """
    db = SessionLocal()
    try:
        config = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
        if config is None:
            raise RuntimeError("No Navidrome credentials saved in database — log in via the UI first.")
        return NavidromeClient(
            url=config.url,
            username=config.username,
            password=config.password,
        )
    finally:
        db.close()


# ─── ListenBrainz candidate gathering ───────────────────────────────────────

def _entry_from_similar(row: dict) -> dict | None:
    """Normalize a Labs similar-recordings row into a resolver entry."""
    artist = (row.get("artist_credit_name") or row.get("artist_name") or "").strip()
    title = (row.get("recording_name") or row.get("track_name") or "").strip()
    mbid = (row.get("recording_mbid") or "").strip() or None
    if not mbid and not (artist and title):
        return None
    return {"artist": artist, "title": title, "recording_mbid": mbid}


async def _gather_lb_candidates(db: Session, client: NavidromeClient) -> list[dict]:
    """Collect ListenBrainz-derived candidate entries, cheapest source first.

    Order matters for the rate limit:
      1. CF recommendations — one call for the whole listening profile, 24h cache
      2. Labs similar-recordings per recent seed — one call each, 7d cache,
         only while short of the candidate target

    Recommendations carry only MBIDs; Labs rows carry names, so those can also
    resolve through the text index. Returns [] when ListenBrainz is
    unconfigured, has no history yet, or errors — the caller then falls back to
    Last.fm.
    """
    cfg = db.query(ListenbrainzConfig).filter(ListenbrainzConfig.id == 1).first()
    if cfg is None:
        return []

    lb = ListenBrainzClient(cfg.user_token, cfg.username)
    try:
        listens = await lb.get_recent_listens(days=LB_RECENCY_DAYS, count=200)
        if not listens:
            logger.info("ListenBrainz: no listens in the last %d days — falling back", LB_RECENCY_DAYS)
            return []
        if len(listens) < MIN_LB_LISTENS:
            logger.info(
                "ListenBrainz: only %d listen(s) (need %d) — treating history as thin",
                len(listens), MIN_LB_LISTENS,
            )

        entries: list[dict] = []

        recs = get_cached_recommendations(db)
        if recs is None:
            recs = await lb.get_recommendations()
            put_recommendations(db, recs)
        for rec in recs:
            mbid = (rec.get("recording_mbid") or "").strip()
            if mbid:
                # No names available here; MBID-only entries resolve via the index.
                entries.append({"artist": "", "title": "", "recording_mbid": mbid})

        seeds_used = 0
        for seed in listens:
            if len(entries) >= LB_CANDIDATE_TARGET or seeds_used >= LB_SEED_LIMIT:
                break
            mbid = seed.get("recording_mbid")
            if not mbid:
                continue
            similar = get_cached_similar(db, mbid)
            if similar is None:
                similar = await lb.get_similar_recordings(mbid)
                put_similar(db, mbid, similar)
            seeds_used += 1
            for row in similar:
                entry = _entry_from_similar(row)
                if entry:
                    entries.append(entry)

        logger.info(
            "ListenBrainz: %d candidate(s) from %d recommendation(s) + %d seed(s) "
            "(of %d listens)",
            len(entries), len(recs), seeds_used, len(listens),
        )
        return entries
    except Exception as e:
        logger.warning("ListenBrainz candidate gathering failed: %s", e)
        return []
    finally:
        await lb.close()


async def _resolve_via_search(client: NavidromeClient, unresolved, cap: int = LB_SEARCH_FALLBACK_CAP) -> list[str]:
    """Last resort: ask Navidrome to search for entries nothing else matched.

    Capped because each call hits Navidrome, and MBID-only entries (no names)
    have nothing to search on anyway.
    """
    found: list[str] = []
    for entry in unresolved[:cap]:
        artist = (entry.get("artist") or "").strip()
        title = (entry.get("title") or "").strip()
        if not artist or not title:
            continue
        try:
            result = await client.search(
                f"{artist} {title}", artist_count=0, album_count=0, song_count=1,
            )
            songs = (result or {}).get("searchResult3", {}).get("song", [])
            if songs:
                found.append(songs[0]["id"])
        except Exception as e:
            logger.debug("search fallback failed for '%s - %s': %s", artist, title, e)
    return found


async def _lb_similar_for_track(
    db: Session,
    lb: ListenBrainzClient,
    client: NavidromeClient,
    song_id: str,
    limit: int = 20,
) -> list[str]:
    """ListenBrainz similar tracks for one local track, resolved to Navidrome ids.

    Requires the track's recording MBID from the local index (captured at sync
    time), so this costs one Labs call at most — cached for 7 days.
    """
    row = db.query(Track).filter(Track.id == song_id).first()
    mbid = (getattr(row, "recording_mbid", None) or "").strip() if row else ""
    if not mbid:
        return []

    try:
        rows = get_cached_similar(db, mbid)
        if rows is None:
            rows = await lb.get_similar_recordings(mbid)
            put_similar(db, mbid, rows)
    except Exception as e:
        logger.warning("ListenBrainz similarity failed for track %s: %s", song_id, e)
        return []

    entries = [entry for entry in (_entry_from_similar(r) for r in rows) if entry]
    if not entries:
        return []

    ids, unresolved = resolve_entries(db, entries)
    ids.extend(await _resolve_via_search(client, unresolved))
    return list(dict.fromkeys(ids))[:limit]


# ─── Job Functions ───────────────────────────────────────────────────────────

async def run_recency_trigger(trigger_id: int):
    """Recency Trigger: generate a 50-track Daily Mix playlist.

    Sources (when Last.fm is configured):
      - 50%: songs similar to last 7 days of Last.fm scrobbles
      - 50%: recently added tracks from Navidrome
    Falls back to 100% recently added if Last.fm is not configured.
    """
    logger.info(f"Running recency trigger {trigger_id}")
    db = SessionLocal()
    try:
        trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
        if not trigger or not trigger.enabled:
            return

        client = _get_client_from_db()
        try:
            # Determine playlist name and derive cooldown lookup prefix
            playlist_name = trigger.playlist_name or f"Fresh Discoveries {datetime.utcnow().strftime('%Y-%m-%d')}"
            cooldown_prefix = _cooldown_prefix_for(playlist_name)
            blocked = _get_cooldown_track_ids(db, cooldown_prefix, days=2)

            # ── Gather candidate tracks ──
            similar_from_lb: list[str] = []       # PRIMARY: ListenBrainz
            similar_from_nav: list[str] = []      # PATH A: Navidrome similar songs
            similar_from_lastfm: list[str] = []   # PATH B: Last.fm similar -> Navidrome lookup
            newest_ids: list[str] = []

            # ── ListenBrainz first — it is the primary history source ──
            lb_entries = await _gather_lb_candidates(db, client)
            if lb_entries:
                lb_ids, unresolved = resolve_entries(db, lb_entries)
                lb_ids.extend(await _resolve_via_search(client, unresolved))
                similar_from_lb = [sid for sid in dict.fromkeys(lb_ids) if sid not in blocked]
                logger.info(
                    f"ListenBrainz: {len(lb_entries)} candidates -> "
                    f"{len(similar_from_lb)} usable tracks"
                )

                # Navidrome's own similarity is local and free, so gather it for a
                # few resolved seeds even when ListenBrainz covers the run. Without
                # this the history side would be 100% ListenBrainz and the blend
                # would lose its 40% "safe" Navidrome component.
                for seed_id in similar_from_lb[:5]:
                    try:
                        nav_songs = await client.get_similar_songs2(seed_id, count=20)
                    except Exception as e:
                        logger.debug(f"Navidrome similarity failed for seed {seed_id}: {e}")
                        continue
                    similar_from_nav.extend(
                        s["id"] for s in (nav_songs or []) if s.get("id")
                    )
                similar_from_nav = [
                    sid for sid in dict.fromkeys(similar_from_nav) if sid not in blocked
                ]

            # ── Last.fm only when ListenBrainz produced nothing ──
            lastfm_cfg = None if similar_from_lb else db.query(LastfmConfig).filter(LastfmConfig.id == 1).first()
            if similar_from_lb:
                logger.info("ListenBrainz covered this run — Last.fm skipped")
            elif lastfm_cfg:
                try:
                    lfm = LastfmClient(lastfm_cfg.api_key, lastfm_cfg.username)
                    try:
                        recent = await lfm.get_recent_tracks(days=7, limit=200)
                        if recent:
                            searched = 0
                            for entry in recent[:20]:  # limit to 20 scrobble seeds
                                try:
                                    # SEED TRACK: Search Navidrome for the scrobbled track
                                    search_result = await client.search(
                                        query=f'{entry["artist"]} {entry["title"]}',
                                        artist_count=0,
                                        album_count=0,
                                        song_count=3,
                                    )
                                    song_hits = search_result.get("searchResult3", {}).get("song", [])
                                    if not song_hits:
                                        continue
                                    seed_id = song_hits[0]["id"]
            
                                    # PATH A: Navidrome similar songs
                                    similar = await client.get_similar_songs2(seed_id, count=20)
                                    for s in similar:
                                        sid = s["id"]
                                        if sid not in similar_from_nav and sid not in similar_from_lastfm:
                                            similar_from_nav.append(sid)
            
                                    # PATH B: Last.fm similar songs -> Navidrome lookup
                                    lfm_similar = await lfm.get_similar(entry["artist"], entry["title"], limit=50)
                                    for lfm_track in lfm_similar[:15]:  # Check top 15
                                        lfm_artist = lfm_track.get("artist", {}).get("#text", "")
                                        lfm_title = lfm_track.get("name", "")
                                        if lfm_artist and lfm_title:
                                            try:
                                                nav_hits = await client.search(
                                                    query=f'{lfm_artist} {lfm_title}',
                                                    artist_count=0,
                                                    album_count=0,
                                                    song_count=1,
                                                )
                                                if nav_hits:
                                                    nav_id = nav_hits[0]["id"]
                                                    if nav_id not in similar_from_nav and nav_id not in similar_from_lastfm:
                                                        similar_from_lastfm.append(nav_id)
                                            except Exception:
                                                continue
                                    searched += 1
                                except Exception as e:
                                    logger.debug(f"Search/similar failed for '{entry['artist']} - {entry['title']}': {e}")
                                    continue
                            logger.info(f"Last.fm: {searched} seeds processed, {len(similar_from_nav)} nav-similar + {len(similar_from_lastfm)} lastfm-similar tracks collected")
                    finally:
                        await lfm.close()
                except Exception as e:
                    logger.warning(f"Last.fm fetch failed: {e}")
            else:
                logger.info("Neither ListenBrainz nor Last.fm configured — using newest tracks only")

            # Apply 2-day cooldown: exclude tracks already in recent same-named playlists
            similar_from_nav = [sid for sid in similar_from_nav if sid not in blocked]
            similar_from_lastfm = [sid for sid in similar_from_lastfm if sid not in blocked]

            # Recently added tracks (40% of playlist) with per-artist diversity
            newest_ids = []
            # How many newest tracks to gather. Thin history — a fresh
            # ListenBrainz account, or Last.fm unset — must not shrink the
            # playlist, so gather enough newest tracks to fill it outright.
            history_available = len(similar_from_lb) + len(similar_from_nav) + len(similar_from_lastfm)
            newest_target = 20 if history_available >= LB_CANDIDATE_TARGET else 50
            if history_available < LB_CANDIDATE_TARGET:
                logger.info(
                    f"Thin history ({history_available} history tracks) — bootstrap mode: "
                    f"gathering up to {newest_target} newest tracks"
                )
            artist_counts = {}  # track how many tracks we've taken per artist
            max_per_artist = 4  # cap per artist to spread variety
            try:
                recent_albums = await client.get_album_list2(type_="newest", size=50)
                if recent_albums:
                    for album_data in recent_albums:
                        try:
                            album_detail = await client.get_album(album_data["id"])
                            for song in album_detail.get("album", {}).get("song", []):
                                if len(newest_ids) >= newest_target:
                                    break
                                song_id = song["id"]
                                artist_name = song.get("artist_name", "").strip()
                                if not artist_name:
                                    # fallback to unknown
                                    artist_name = "Unknown Artist"
                                if song_id in newest_ids:
                                    continue
                                current = artist_counts.get(artist_name, 0)
                                if current >= max_per_artist:
                                    continue
                                # also avoid duplicates with similar pools
                                if song_id in similar_from_lb or song_id in similar_from_nav or song_id in similar_from_lastfm:
                                    continue
                                newest_ids.append(song_id)
                                artist_counts[artist_name] = current + 1
                        except Exception as e:
                            logger.warning(f"Failed to get album {album_data['id']}: {e}")
                            continue
                logger.info(f"Newest tracks collected: {len(newest_ids)} (from {len(artist_counts)} artists)")
            except Exception as e:
                logger.warning(f"Failed to fetch newest albums: {e}")

            # Fallback: if per-artist diversity left us short, fill rest with same per-artist cap
            if len(newest_ids) < newest_target:
                needed = newest_target - len(newest_ids)
                try:
                    more_albums = await client.get_album_list2(type_="newest", size=50)
                    if more_albums:
                        for album_data in more_albums:
                            if len(newest_ids) >= newest_target:
                                break
                            try:
                                album_detail = await client.get_album(album_data["id"])
                                for song in album_detail.get("album", {}).get("song", []):
                                    if len(newest_ids) >= newest_target:
                                        break
                                    sid = song["id"]
                                    artist_name = song.get("artist_name", "").strip() or "Unknown Artist"
                                    if artist_counts.get(artist_name, 0) >= max_per_artist:
                                        continue
                                    if sid not in newest_ids and sid not in similar_from_nav and sid not in similar_from_lastfm:
                                        newest_ids.append(sid)
                                        artist_counts[artist_name] = artist_counts.get(artist_name, 0) + 1
                            except Exception:
                                continue
                except Exception as e:
                    logger.warning(f"Fallback newests failed: {e}")
                logger.info(f"Newest tracks after fallback: {len(newest_ids)} (from {len(artist_counts)} artists)")

            # Apply cooldown to the assembled newest pool (in case the per-artist fallback added blocked IDs)
            newest_ids = [sid for sid in newest_ids if sid not in blocked]

            # --- Build the playlist: ListenBrainz-primary blend ---
            # 60% listening history / 40% newest; within history ListenBrainz
            # holds 60% and Navidrome similarity 40%, Last.fm only fills gaps.
            #
            # The RNG is seeded from (trigger, playlist name) so re-running the
            # same day's trigger reproduces the same playlist instead of churning
            # it — the dated name is the identity of the run.
            rng = seeded_rng("recency", trigger.id, playlist_name)
            blended = _blend_tracks(
                lb_similar=similar_from_lb,
                nav_similar=similar_from_nav,
                lfm_similar=similar_from_lastfm,
                newest_ids=newest_ids,
                target=50,
                rng=rng,
            )
            track_ids = _curate(db, blended, target=50, rng=rng)
            logger.info(
                f"Blend: {len(similar_from_lb)} LB + {len(similar_from_nav)} nav + "
                f"{len(similar_from_lastfm)} lastfm history, {len(newest_ids)} newest "
                f"-> {len(blended)} blended -> {len(track_ids)} after curation"
            )
            # ── Push to Navidrome ────────────────────────────────────────────
            published = await _publish_playlist(
                db, client, playlist_name, track_ids, prune=True
            )
            if not published:
                # Publish gate rejected the run; yesterday's playlist stays put.
                trigger.last_run = datetime.utcnow()
                db.commit()
                return

            trigger.last_run = datetime.utcnow()
            db.commit()
            logger.info(f"Trigger '{trigger.name}': created/updated '{playlist_name}' with {len(track_ids)} tracks")
        finally:
            await client.close()
    except Exception as e:
        db.rollback()
        logger.error(f"Recency trigger {trigger_id} failed: {e}")
    finally:
        db.close()


async def run_heavy_rotation_trigger(trigger_id: int):
    """Heavy Rotation Trigger: when a song crosses a play threshold, generate a companion playlist."""
    logger.info(f"Running heavy rotation trigger {trigger_id}")
    db = SessionLocal()
    try:
        trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
        if not trigger or not trigger.enabled:
            return

        threshold = trigger.threshold or 5
        client = _get_client_from_db()
        # ListenBrainz client (primary similarity source) — one instance for the
        # whole run so its rate limiter is shared across seeds.
        lb_client = None
        try:
            lb_cfg = db.query(ListenbrainzConfig).filter(ListenbrainzConfig.id == 1).first()
            if lb_cfg:
                lb_client = ListenBrainzClient(lb_cfg.user_token, lb_cfg.username)
        except Exception:
            pass
        # Last.fm client for diverse similarity lookups (fallback only)
        lastfm_client = None
        try:
            lastfm_cfg = db.query(LastfmConfig).filter(LastfmConfig.id == 1).first()
            if lastfm_cfg:
                lastfm_client = LastfmClient(lastfm_cfg.api_key, lastfm_cfg.username)
        except Exception:
            pass
        try:
            recent_albums = await client.get_album_list2(type_="frequent", size=50)
            hot_track_ids = []
            hot_track_names = []
            hot_track_artists = []

            for album_data in recent_albums:
                try:
                    album_detail = await client.get_album(album_data["id"])
                    for song in album_detail.get("album", {}).get("song", []):
                        if song.get("playCount", 0) >= threshold:
                            hot_track_ids.append(song["id"])
                            hot_track_names.append(song.get("title", "Unknown"))
                            hot_track_artists.append(song.get("artist", ""))
                except Exception as e:
                    logger.warning(f"Failed to get album {album_data['id']}: {e}")
                    continue

            if not hot_track_ids:
                logger.info(f"No tracks exceeded play threshold {threshold}")
                return

            for track_id, track_name, track_artist in list(zip(hot_track_ids, hot_track_names, hot_track_artists))[:5]:
                try:
                    # ── Gather candidate tracks ──────────────────────────────────────
                    similar_from_lb: list[str] = []       # PRIMARY: ListenBrainz
                    similar_from_nav: list[str] = []      # PATH A: Navidrome similar songs
                    similar_from_lastfm: list[str] = []   # PATH B: Last.fm similar songs (cached)

                    # PRIMARY: ListenBrainz similar-recordings for this seed
                    if lb_client is not None:
                        try:
                            similar_from_lb = await _lb_similar_for_track(
                                db, lb_client, client, track_id, limit=20,
                            )
                            if similar_from_lb:
                                logger.debug(
                                    f"ListenBrainz similarity for '{track_name}': "
                                    f"{len(similar_from_lb)} tracks"
                                )
                        except Exception as e:
                            logger.warning(f"ListenBrainz similarity failed for '{track_name}': {e}")

                    # PATH A: Navidrome similar songs
                    try:
                        nav_songs = await client.get_similar_songs2(track_id, count=50)
                        if nav_songs:
                            similar_from_nav = [s["id"] for s in nav_songs]
                            logger.debug(f"Navidrome similarity for '{track_name}': {len(similar_from_nav)} tracks")
                    except Exception as e:
                        logger.warning(f"Navidrome similarity failed for '{track_name}': {e}")

                    # PATH B: Last.fm similarity — fallback only, skipped when LB covered it
                    if lastfm_client and not similar_from_lb:
                        try:
                            lfm_similar = await lastfm_client.get_similar(track_artist, track_name, limit=50)
                            if lfm_similar:
                                for lfm_track in lfm_similar[:15]:  # Check top 15
                                    lfm_artist = lfm_track.get("artist", {}).get("#text", "")
                                    lfm_title = lfm_track.get("name", "")
                                    if lfm_artist and lfm_title:
                                        try:
                                            search_result = await client.search(
                                                query=f'{lfm_artist} {lfm_title}',
                                                artist_count=0,
                                                album_count=0,
                                                song_count=1,
                                            )
                                            # search3.view returns the response envelope,
                                            # not a bare list — indexing it raised KeyError
                                            # and the except below silently skipped it.
                                            hits = (search_result or {}).get(
                                                "searchResult3", {}
                                            ).get("song", [])
                                            if hits:
                                                nav_id = hits[0]["id"]
                                                if nav_id not in similar_from_nav and nav_id not in similar_from_lastfm:
                                                    similar_from_lastfm.append(nav_id)
                                        except Exception:
                                            continue
                                logger.debug(f"Last.fm similarity for '{track_name}': {len(similar_from_lastfm)} tracks matched in Navidrome")
                        except Exception as e:
                            logger.warning(f"Last.fm similarity failed for '{track_name}': {e}")

                    # If we got nothing from any source, skip this seed
                    if not similar_from_lb and not similar_from_nav and not similar_from_lastfm:
                        logger.warning(f"No similarity results for '{track_name}' from any source")
                        continue

                    # Blend per-seed sources: ListenBrainz primary (60%),
                    # Navidrome the safe remainder (40%), Last.fm only fills gaps.
                    target_per_seed = 20
                    sim_ids = _blend_tracks(
                        lb_similar=similar_from_lb,
                        nav_similar=similar_from_nav,
                        lfm_similar=similar_from_lastfm,
                        newest_ids=[],
                        target=target_per_seed,
                        newest_share=0.0,
                    )

                    logger.info(
                        f"Seed '{track_name}': {len(similar_from_lb)} LB + "
                        f"{len(similar_from_nav)} Navidrome + "
                        f"{len(similar_from_lastfm)} Last.fm -> {len(sim_ids)} tracks"
                    )
                    playlist_name = trigger.playlist_name or f"More Like This: {track_name}"

                    existing = db.query(GeneratedPlaylist).filter(
                        GeneratedPlaylist.name == playlist_name
                    ).first()

                    if existing and existing.navidrome_playlist_id:
                        await client.update_playlist(existing.navidrome_playlist_id, sim_ids, name=playlist_name)
                        existing.track_ids = json.dumps(sim_ids)
                        existing.track_count = len(sim_ids)
                        existing.updated_at = datetime.utcnow()
                        logger.info(f"Trigger '{trigger.name}': updated '{playlist_name}' with {len(sim_ids)} tracks (seed: {track_name})")
                    else:
                        result = await client.create_playlist(playlist_name, sim_ids)
                        playlist_id = result.get("playlist", {}).get("id", "")
                        playlist = GeneratedPlaylist(
                            name=playlist_name,
                            navidrome_playlist_id=playlist_id,
                            seed_track_id=track_id,
                            seed_track_name=track_name,
                            track_count=len(sim_ids),
                            track_ids=json.dumps(sim_ids),
                        )
                        db.add(playlist)
                        logger.info(f"Trigger '{trigger.name}': created '{playlist_name}' with {len(sim_ids)} tracks (seed: {track_name})")
                except Exception as e:
                    logger.warning(f"Failed to generate playlist for hot track {track_name}: {e}")
                    continue

            trigger.last_run = datetime.utcnow()
            db.commit()
            logger.info(f"Heavy rotation trigger processed {len(hot_track_ids)} hot tracks")
        finally:
            if lastfm_client:
                await lastfm_client.close()
            if lb_client is not None:
                await lb_client.close()
            await client.close()
    except Exception as e:
        db.rollback()
        logger.error(f"Heavy rotation trigger {trigger_id} failed: {e}")
    finally:
        db.close()


async def run_scheduled_trigger(trigger_id: int):
    """Scheduled Trigger: refresh a named playlist based on top-starred/frequent tracks."""
    logger.info(f"Running scheduled trigger {trigger_id}")
    db = SessionLocal()
    try:
        trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
        if not trigger or not trigger.enabled:
            return

        client = _get_client_from_db()
        try:
            frequent_albums = await client.get_album_list2(type_="frequent", size=20)
            track_ids = []

            for album_data in frequent_albums:
                try:
                    album_detail = await client.get_album(album_data["id"])
                    for song in album_detail.get("album", {}).get("song", []):
                        track_ids.append(song["id"])
                    if len(track_ids) >= 50:
                        break
                except Exception as e:
                    logger.warning(f"Failed to get album {album_data['id']}: {e}")
                    continue

            if not track_ids:
                logger.info("No tracks found for scheduled trigger")
                return

            playlist_name = trigger.playlist_name or "Scheduled Mix"

            published = await _publish_playlist(db, client, playlist_name, track_ids)
            if not published:
                trigger.last_run = datetime.utcnow()
                db.commit()
                return

            trigger.last_run = datetime.utcnow()
            db.commit()
            logger.info(f"Trigger '{trigger.name}': refreshed '{playlist_name}' with {len(track_ids)} tracks")
        finally:
            await client.close()
    except Exception as e:
        db.rollback()
        logger.error(f"Scheduled trigger {trigger_id} failed: {e}")
    finally:
        db.close()


async def run_library_mix_trigger(trigger_id: int):
    """Library Mix Trigger: publish one of the local-only mixes.

    Everything here is computed from the local library and play counts — no
    similarity API, no rate limit, no listening-history window. That is what
    makes these mixes reliable: they cannot come back empty because an upstream
    service is thin or down, only because the library genuinely lacks the data.

    Which mix to build comes from `trigger.mix_kind` (see services/mixes.py).
    The playlist name defaults to the mix's label, undated, so each run replaces
    the previous version instead of piling up one playlist per day.
    """
    logger.info(f"Running library mix trigger {trigger_id}")
    db = SessionLocal()
    try:
        trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
        if not trigger or not trigger.enabled:
            return

        kind = (trigger.mix_kind or DEFAULT_MIX).strip().lower()
        spec = MIX_SPECS.get(kind) or MIX_SPECS[DEFAULT_MIX]
        playlist_name = trigger.playlist_name or spec["label"]

        # Mixes are per user: everything they read (plays, last played) is a
        # personal annotation, so a trigger with no owner has nobody to build
        # for. Refuse rather than silently picking someone else's history.
        owner_id = trigger.owner_id
        if owner_id is None:
            logger.error("Trigger %s has no owner — refusing to run", trigger.id)
            return

        # Weekly seed on the undated name: a stable playlist within the week,
        # a different draw next week, and no churn if the job reruns.
        iso_year, iso_week, _ = datetime.utcnow().isocalendar()
        rng = seeded_rng("library_mix", trigger.id, kind, f"{iso_year}-W{iso_week}")

        entries = select_mix(db, owner_id, kind, rng=rng)
        track_ids = _curate(
            db, [entry["id"] for entry in entries], target=spec.get("limit", 50), rng=rng
        )
        logger.info(
            f"Library mix '{kind}': {len(entries)} candidate(s) -> "
            f"{len(track_ids)} track(s) after curation"
        )

        if not track_ids:
            # An empty mix is a real answer (no play history yet, nothing
            # unplayed, ...) — leave whatever is already in Navidrome alone.
            logger.info(f"Library mix '{kind}' produced nothing — nothing published")
            trigger.last_run = datetime.utcnow()
            db.commit()
            return

        client = _get_client_from_db()
        try:
            await _publish_playlist(db, client, playlist_name, track_ids)
        finally:
            await client.close()

        trigger.last_run = datetime.utcnow()
        db.commit()
        logger.info(
            f"Trigger '{trigger.name}': published '{playlist_name}' "
            f"({kind}) with {len(track_ids)} tracks"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Library mix trigger {trigger_id} failed: {e}")
    finally:
        db.close()


# Trigger type → job function mapping
TRIGGER_JOBS = {
    "recency": run_recency_trigger,
    "heavy_rotation": run_heavy_rotation_trigger,
    "scheduled": run_scheduled_trigger,
    "library_mix": run_library_mix_trigger,
}


# ─── Job Management ──────────────────────────────────────────────────────────

def add_trigger_job(trigger: SmartTrigger):
    """Register an APScheduler job for a trigger."""
    sched = _get_scheduler()
    job_id = _job_id(trigger)

    if trigger.trigger_type not in TRIGGER_JOBS:
        logger.warning(f"Unknown trigger type: {trigger.trigger_type}")
        return

    job_func = TRIGGER_JOBS[trigger.trigger_type]

    if trigger.trigger_type == "scheduled" and trigger.cron_expression:
        parts = trigger.cron_expression.strip().split(" ")
        if len(parts) == 5:
            sched.add_job(
                job_func,
                trigger=CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                ),
                id=job_id,
                args=[trigger.id],
                replace_existing=True,
            )
        else:
            logger.warning(f"Invalid cron expression for trigger {trigger.id}: {trigger.cron_expression}")
            return
    elif trigger.trigger_type == "library_mix" and trigger.cron_expression:
        # Mixes are cheap (local queries only), but the seed rotates weekly, so
        # a daily run is the useful default and a custom cron is honoured.
        parts = trigger.cron_expression.strip().split(" ")
        if len(parts) != 5:
            logger.warning(
                f"Invalid cron expression for library_mix trigger {trigger.id}: "
                f"{trigger.cron_expression}"
            )
            return
        sched.add_job(
            job_func,
            trigger=CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            ),
            id=job_id,
            args=[trigger.id],
            replace_existing=True,
        )
    elif trigger.trigger_type == "library_mix":
        sched.add_job(
            job_func,
            trigger=CronTrigger(hour=6, minute=30),
            id=job_id,
            args=[trigger.id],
            replace_existing=True,
        )
    elif trigger.trigger_type == "recency":
        sched.add_job(
            job_func,
            trigger=CronTrigger(hour=9, minute=0),
            id=job_id,
            args=[trigger.id],
            replace_existing=True,
        )
    elif trigger.trigger_type == "heavy_rotation":
        sched.add_job(
            job_func,
            trigger=CronTrigger(hour="*/6"),
            id=job_id,
            args=[trigger.id],
            replace_existing=True,
        )

    if not trigger.enabled:
        sched.pause_job(job_id)

    logger.info(f"Added scheduler job {job_id} for trigger '{trigger.name}'")


def remove_trigger_job(trigger_id: int):
    """Remove an APScheduler job for a trigger."""
    sched = _get_scheduler()
    job_id = f"trigger-{trigger_id}"
    try:
        sched.remove_job(job_id)
        logger.info(f"Removed scheduler job {job_id}")
    except Exception:
        pass


def pause_trigger_job(trigger_id: int):
    """Pause a trigger's scheduler job."""
    sched = _get_scheduler()
    job_id = f"trigger-{trigger_id}"
    try:
        sched.pause_job(job_id)
        logger.info(f"Paused scheduler job {job_id}")
    except Exception:
        pass


def resume_trigger_job(trigger_id: int):
    """Resume a trigger's scheduler job."""
    sched = _get_scheduler()
    job_id = f"trigger-{trigger_id}"
    try:
        sched.resume_job(job_id)
        logger.info(f"Resumed scheduler job {job_id}")
    except Exception:
        pass
