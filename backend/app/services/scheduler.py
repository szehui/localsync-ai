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
from app.models.database import SmartTrigger, GeneratedPlaylist, ConnectionConfig, SessionLocal, Track, LastfmConfig
from app.services.navidrome import NavidromeClient
from app.services.lastfm import LastfmClient
from app.routers.playlists import _passes_strictness

logger = logging.getLogger(__name__)

# Reference to the global scheduler instance (set from main.py)
_scheduler = None


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
            similar_from_nav: list[str] = []      # PATH A: Navidrome similar songs
            similar_from_lastfm: list[str] = []   # PATH B: Last.fm similar -> Navidrome lookup
            newest_ids: list[str] = []

            # Try Last.fm-sourced similar tracks
            lastfm_cfg = db.query(LastfmConfig).filter(LastfmConfig.id == 1).first()
            if lastfm_cfg:
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
                logger.info("Last.fm not configured - using 100% recently added tracks")

            # Apply 2-day cooldown: exclude tracks already in recent same-named playlists
            similar_from_nav = [sid for sid in similar_from_nav if sid not in blocked]
            similar_from_lastfm = [sid for sid in similar_from_lastfm if sid not in blocked]

            # Recently added tracks (40% of playlist) with per-artist diversity
            newest_ids = []
            artist_counts = {}  # track how many tracks we've taken per artist
            newest_target = int(0.4 * 50)  # 20 from newest (defined early to avoid ordering issue)
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
                                if song_id in similar_from_nav or song_id in similar_from_lastfm:
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

            # --- Build 50-track playlist with weighted blend ---
            #  60% listening history / 40% newest
            #  Within similar: 60% Last.fm / 40% Navidrome
            target = 50
            similar_total_target = int(0.6 * target)        # 30 from listening history
            newest_target = int(0.4 * target)               # 20 from newest
            lastfm_target = int(0.6 * similar_total_target) # 18 from Last.fm
            nav_target = similar_total_target - lastfm_target # 12 from Navidrome

            random.shuffle(similar_from_nav)
            random.shuffle(similar_from_lastfm)
            random.shuffle(newest_ids)

            # Select up to targets from each pool
            selected_nav = similar_from_nav[:nav_target]
            selected_lastfm = similar_from_lastfm[:lastfm_target]
            selected_newest = newest_ids[:newest_target]

            # Build the final playlist with two fallback stages:
            # 1) If a similar sub-pool is short, fill from the other similar sub-pool
            # 2) If newest pool is short, fill from remaining similar tracks (keeps diversity)
            similar_selected = selected_nav + selected_lastfm
            if len(similar_selected) < nav_target + lastfm_target:
                needed = (nav_target + lastfm_target) - len(similar_selected)
                remaining_nav = [x for x in similar_from_nav if x not in selected_nav]
                remaining_lastfm = [x for x in similar_from_lastfm if x not in selected_lastfm]
                fill = (remaining_nav + remaining_lastfm)[:needed]
                similar_selected.extend(fill)

            # If newest pool is short, expand similar pool (keeps artist diversity high)
            if len(selected_newest) < newest_target:
                gap = newest_target - len(selected_newest)
                remaining = [x for x in similar_from_nav + similar_from_lastfm
                            if x not in similar_selected]
                random.shuffle(remaining)
                similar_selected.extend(remaining[:gap])

            # Final assembly: similar + newest, interleaved
            random.shuffle(similar_selected)
            random.shuffle(selected_newest)
            track_ids = (similar_selected + selected_newest)[:target]
            random.shuffle(track_ids)
            # ── Push to Navidrome ────────────────────────────────────────────
            # playlist_name was computed at the top of the function (used for cooldown prefix too)

            existing = db.query(GeneratedPlaylist).filter(
                GeneratedPlaylist.name == playlist_name
            ).first()

            if existing and existing.navidrome_playlist_id:
                # Delete the old Navidrome playlist first (updatePlaylist appends, doesn't replace)
                try:
                    await client.delete_playlist(existing.navidrome_playlist_id)
                except Exception as e:
                    logger.warning(f"Failed to delete old playlist: {e}")
                result = await client.create_playlist(playlist_name, track_ids)
                new_playlist_id = result.get("playlist", {}).get("id", "")
                existing.navidrome_playlist_id = new_playlist_id
                existing.track_ids = json.dumps(track_ids)
                existing.track_count = len(track_ids)
                existing.updated_at = datetime.utcnow()
            else:
                result = await client.create_playlist(playlist_name, track_ids)
                playlist_id = result.get("playlist", {}).get("id", "")
                playlist = GeneratedPlaylist(
                    name=playlist_name,
                    navidrome_playlist_id=playlist_id,
                    track_count=len(track_ids),
                    track_ids=json.dumps(track_ids),
                )
                db.add(playlist)

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
        # Initialize Last.fm client for diverse similarity lookups
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
                    similar_from_nav: list[str] = []      # PATH A: Navidrome similar songs
                    similar_from_lastfm: list[str] = []   # PATH B: Last.fm similar songs (cached)

                    # PATH A: Navidrome similar songs
                    try:
                        nav_songs = await client.get_similar_songs2(track_id, count=50)
                        if nav_songs:
                            similar_from_nav = [s["id"] for s in nav_songs]
                            logger.debug(f"Navidrome similarity for '{track_name}': {len(similar_from_nav)} tracks")
                    except Exception as e:
                        logger.warning(f"Navidrome similarity failed for '{track_name}': {e}")

                    # PATH B: Last.fm similarity (cached) -> Navidrome lookup
                    if lastfm_client:
                        try:
                            lfm_similar = await lastfm_client.get_similar(track_artist, track_name, limit=50)
                            if lfm_similar:
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
                                logger.debug(f"Last.fm similarity for '{track_name}': {len(similar_from_lastfm)} tracks matched in Navidrome")
                        except Exception as e:
                            logger.warning(f"Last.fm similarity failed for '{track_name}': {e}")

                    # If we got nothing from either source, skip this seed
                    if not similar_from_nav and not similar_from_lastfm:
                        logger.warning(f"No similarity results for '{track_name}' from any source")
                        continue

                    # Blend the two sources: prefer Last.fm for diversity, fallback to Navidrome
                    # We want 20 tracks total per seed track
                    target_per_seed = 20
                    lastfm_weight = 0.6   # 60% from Last.fm (more diverse)
                    nav_weight = 0.4      # 40% from Navidrome (more conservative)

                    lastfm_target = int(target_per_seed * lastfm_weight)   # 12
                    nav_target = target_per_seed - lastfm_target          # 8

                    # Shuffle both lists for variety
                    random.shuffle(similar_from_nav)
                    random.shuffle(similar_from_lastfm)

                    # Take from each source
                    selected_nav = similar_from_nav[:nav_target]
                    selected_lastfm = similar_from_lastfm[:lastfm_target]

                    # If one source is short, fill from the other
                    combined = selected_nav + selected_lastfm
                    if len(combined) < target_per_seed:
                        needed = target_per_seed - len(combined)
                        # Fill from remaining Navidrome tracks
                        if len(similar_from_nav) > len(selected_nav):
                            remaining_nav = similar_from_nav[len(selected_nav):]
                            take = min(needed, len(remaining_nav))
                            combined.extend(remaining_nav[:take])
                            needed -= take
                        # Fill from remaining Last.fm tracks
                        if needed > 0 and len(similar_from_lastfm) > len(selected_lastfm):
                            remaining_lastfm = similar_from_lastfm[len(selected_lastfm):]
                            take = min(needed, len(remaining_lastfm))
                            combined.extend(remaining_lastfm[:take])

                    # Final trim and shuffle
                    final_similar = combined[:target_per_seed]
                    random.shuffle(final_similar)
                    sim_ids = final_similar

                    logger.info(f"Seed '{track_name}': {len(selected_lastfm)} Last.fm + {len(selected_nav)} Navidrome = {len(sim_ids)} tracks")
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

            existing = db.query(GeneratedPlaylist).filter(
                GeneratedPlaylist.name == playlist_name
            ).first()

            if existing and existing.navidrome_playlist_id:
                await client.update_playlist(existing.navidrome_playlist_id, track_ids, name=playlist_name)
                existing.track_ids = json.dumps(track_ids)
                existing.track_count = len(track_ids)
                existing.updated_at = datetime.utcnow()
            else:
                result = await client.create_playlist(playlist_name, track_ids)
                playlist_id = result.get("playlist", {}).get("id", "")
                playlist = GeneratedPlaylist(
                    name=playlist_name,
                    navidrome_playlist_id=playlist_id,
                    track_count=len(track_ids),
                    track_ids=json.dumps(track_ids),
                )
                db.add(playlist)

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


# Trigger type → job function mapping
TRIGGER_JOBS = {
    "recency": run_recency_trigger,
    "heavy_rotation": run_heavy_rotation_trigger,
    "scheduled": run_scheduled_trigger,
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
        parts = trigger.cron_expression.strip().split()
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
