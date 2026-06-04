"""Smart Triggers scheduler service.

Manages APScheduler jobs for each enabled trigger in the database.
When triggers are created, updated, deleted, or toggled, the corresponding
scheduler job is added, modified, removed, or paused.
"""
import json
import logging
from datetime import datetime
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from app.models.database import SmartTrigger, GeneratedPlaylist, SessionLocal, Track
from app.services.navidrome import NavidromeClient
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


# ─── Job Functions ───────────────────────────────────────────────────────────

async def run_recency_trigger(trigger_id: int):
    """Recency Trigger: generate a 'Fresh Discoveries' playlist from recently added tracks."""
    logger.info(f"Running recency trigger {trigger_id}")
    db = SessionLocal()
    try:
        trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
        if not trigger or not trigger.enabled:
            return

        client = NavidromeClient()
        try:
            recent_albums = await client.get_album_list2(type_="newest", size=50)
            if not recent_albums:
                logger.info("No recent albums found for recency trigger")
                return

            track_ids = []
            for album_data in recent_albums[:10]:
                try:
                    album_detail = await client.get_album(album_data["id"])
                    for song in album_detail.get("song", []):
                        track_ids.append(song["id"])
                except Exception as e:
                    logger.warning(f"Failed to get album {album_data['id']}: {e}")
                    continue

            if not track_ids:
                logger.info("No tracks found in recent albums")
                return

            playlist_name = trigger.playlist_name or f"Fresh Discoveries {datetime.utcnow().strftime('%Y-%m-%d')}"

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
            logger.info(f"Recency trigger created/updated '{playlist_name}' with {len(track_ids)} tracks")
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
        client = NavidromeClient()
        try:
            recent_albums = await client.get_album_list2(type_="frequent", size=50)
            hot_track_ids = []
            hot_track_names = []

            for album_data in recent_albums:
                try:
                    album_detail = await client.get_album(album_data["id"])
                    for song in album_detail.get("song", []):
                        if song.get("playCount", 0) >= threshold:
                            hot_track_ids.append(song["id"])
                            hot_track_names.append(song.get("title", "Unknown"))
                except Exception as e:
                    logger.warning(f"Failed to get album {album_data['id']}: {e}")
                    continue

            if not hot_track_ids:
                logger.info(f"No tracks exceeded play threshold {threshold}")
                return

            for track_id, track_name in list(zip(hot_track_ids, hot_track_names))[:5]:
                try:
                    similar = await client.get_similar_songs2(track_id, count=20)
                    if not similar:
                        continue

                    sim_ids = [s["id"] for s in similar[:20]]
                    playlist_name = trigger.playlist_name or f"More Like This: {track_name}"

                    existing = db.query(GeneratedPlaylist).filter(
                        GeneratedPlaylist.name == playlist_name
                    ).first()

                    if existing and existing.navidrome_playlist_id:
                        await client.update_playlist(existing.navidrome_playlist_id, sim_ids, name=playlist_name)
                        existing.track_ids = json.dumps(sim_ids)
                        existing.track_count = len(sim_ids)
                        existing.updated_at = datetime.utcnow()
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
                except Exception as e:
                    logger.warning(f"Failed to generate playlist for hot track {track_name}: {e}")
                    continue

            trigger.last_run = datetime.utcnow()
            db.commit()
            logger.info(f"Heavy rotation trigger processed {len(hot_track_ids)} hot tracks")
        finally:
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

        client = NavidromeClient()
        try:
            frequent_albums = await client.get_album_list2(type_="frequent", size=20)
            track_ids = []

            for album_data in frequent_albums:
                try:
                    album_detail = await client.get_album(album_data["id"])
                    for song in album_detail.get("song", []):
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
            logger.info(f"Scheduled trigger refreshed '{playlist_name}' with {len(track_ids)} tracks")
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
