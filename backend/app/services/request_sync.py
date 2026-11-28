"""Status truth: keep every open request honest against Lidarr and the library.

A request's status is evidence, not intent. `approved` means "we asked Lidarr",
`downloading` means "a download client has it", `imported` means "Lidarr has the
files", `available` means "your library actually serves it". Anything derived
from a *sibling* row is not evidence — `POST /album` makes Lidarr create the
artist and its whole discography metadata, so a matching album row can exist,
unmonitored, having never been asked for. Requests are matched on the
release-group MBID and confirmed by album statistics, never by title.

The ladder is deliberately monotonic (:data:`app.services.requests.STATUS_RANK`):
Lidarr moves a record out of the queue before the importer writes it, so a single
cycle that sees an empty queue and no files must not demote `downloading` to
`approved`. `failed` is the one state that can be left again, but only on
positive evidence (files appeared), never on silence.

Two calls per cycle, not two per request: the queue and the history are fetched
once and indexed by album id, because ten open requests otherwise pull the same
queue ten times.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.database import LidarrConfig, Request, SessionLocal
from app.services.catalog import OwnershipIndex, library_has_release_group, ownership_index
from app.services.lidarr import LidarrService
from app.services.requests import (
    OPEN_STATUSES,
    STATUS_RANK,
)

logger = logging.getLogger(__name__)

# History event types that mean the current attempt is dead. `albumImportIncomplete`
# is deliberately absent: it fires whenever some tracks landed, which the album
# statistics already describe more precisely (`partially_available`).
FAILURE_EVENTS = frozenset({"downloadFailed", "importFailed"})

# A search that found nothing 48h ago is a real failure: Lidarr retries on its
# own schedule and would have tried again by now.
STALE_SEARCH = timedelta(hours=48)

# Failed rows keep being watched for this long, because Lidarr's own RSS/auto
# search can still deliver an album after our attempt failed, and a request that
# quietly becomes available should say so.
FAILED_LOOKBACK = timedelta(days=30)

# Everything the reconciler is willing to touch.
RECONCILE_STATUSES = OPEN_STATUSES + ("failed",)


# ── Parsing and indexing ────────────────────────────────────────────────────


def utc(value: Any) -> Optional[datetime]:
    """Parse Lidarr's timestamps into naive UTC.

    SQLite drops tzinfo on write, so an aware datetime read from the database
    would raise on comparison with the naive values Lidarr hands back. Every
    timestamp in this module is normalised to naive UTC at the boundary.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _percent(album: Optional[dict]) -> int:
    stats = (album or {}).get("statistics") or {}
    try:
        return int(stats.get("percentOfTracks") or 0)
    except (TypeError, ValueError):
        return 0


def _track_count(album: Optional[dict]) -> Optional[int]:
    stats = (album or {}).get("statistics") or {}
    for key in ("totalTrackCount", "trackCount"):
        try:
            value = int(stats.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return None


def queue_album_ids(records: list[dict]) -> set[int]:
    return {rec["albumId"] for rec in records
            if isinstance(rec.get("albumId"), int)}


def failures_by_album(records: list[dict]) -> dict[int, tuple[datetime, str]]:
    """Latest failure per album id: ``{album_id: (when, event_type)}``."""
    out: dict[int, tuple[datetime, str]] = {}
    for rec in records:
        if rec.get("eventType") not in FAILURE_EVENTS:
            continue
        album_id = rec.get("albumId")
        when = utc(rec.get("date"))
        if not isinstance(album_id, int) or when is None:
            continue
        current = out.get(album_id)
        if current is None or when > current[0]:
            out[album_id] = (when, rec["eventType"])
    return out


# ── The decision ────────────────────────────────────────────────────────────


def _may_advance(current: str, target: str) -> bool:
    """Only ever move a request forward, and only on evidence.

    `available` is terminal. A row cannot leave `failed` for `approved` (that is
    what the retry action is for) but can leave it for a state that proves
    something happened.
    """
    if target == current:
        return False
    if current in ("available", "denied", "cancelled"):
        return False
    if target == "failed":
        # Never call an album with files a failure.
        return current not in ("imported", "available")
    if current == "failed":
        return target in ("downloading", "partially_available", "imported", "available")
    return STATUS_RANK.get(target, -1) > STATUS_RANK.get(current, -1)


def decide(
    current: str,
    *,
    available_evidence: Optional[str],
    album: Optional[dict],
    in_queue: bool,
    failure: Optional[tuple[datetime, str]],
    requested_at: Optional[datetime],
    now: datetime,
) -> tuple[str, Optional[str], Optional[str]]:
    """Pure decision: ``(status, available_evidence, last_error)``.

    No database, no network — one branch per row of the transition table, so the
    table can be tested exhaustively without pretending to be Lidarr.
    """
    if available_evidence:
        return "available", available_evidence, None

    if current == "pending":
        # Only the library can move a pending request: it has not been approved,
        # so nothing in Lidarr is evidence *about this request*. In particular a
        # row created as a side effect of adding the artist is not an approval.
        return current, None, None

    if album is None:
        return ("failed", None,
                "Album is no longer in Lidarr — retry to add it again")

    percent = _percent(album)
    if percent >= 100:
        # Files are in Lidarr but Navidrome has not scanned them yet: saying
        # "available" here would be a promise the library cannot keep.
        return "imported", None, None
    if percent > 0:
        return "partially_available", None, None
    if in_queue:
        return "downloading", None, None

    if failure is not None:
        when, event = failure
        if requested_at is None or when >= requested_at:
            return "failed", None, f"Lidarr reported {event}"

    last_search = utc(album.get("lastSearchTime"))
    if last_search is not None and now - last_search > STALE_SEARCH:
        release_date = utc(album.get("releaseDate"))
        if release_date is None or release_date <= now:
            return "failed", None, "No release found for this album"
        # Announced but not out yet — Lidarr is right to have found nothing.
        return current, None, None

    return "approved", None, None


# ── The cycle ───────────────────────────────────────────────────────────────


async def reconcile_open_requests(
    db: Session,
    *,
    lidarr: Optional[LidarrService] = None,
    index: Optional[OwnershipIndex] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Walk every in-flight request once and record what is actually true.

    Returns counts for the log line and the manual-refresh endpoint. Never
    raises because one row misbehaved: a cycle that updates nine requests and
    skips a tenth is better than no cycle at all.
    """
    now = now or datetime.utcnow()
    rows = (
        db.query(Request)
        .filter(Request.status.in_(RECONCILE_STATUSES))
        .order_by(Request.id)
        .all()
    )
    if not rows:
        # An idle household makes no Lidarr calls at all.
        return {"total": 0, "changed": 0, "failed": 0}

    lidarr = lidarr or _lidarr(db)
    if lidarr is None:
        logger.warning("request reconcile skipped: Lidarr is not configured")
        return {"total": 0, "changed": 0, "failed": 0}

    index = index or ownership_index(db)

    # One fetch each, shared by every request in this cycle.
    try:
        in_queue = queue_album_ids(await lidarr.get_queue())
    except Exception as exc:  # noqa: BLE001 - a missing queue degrades, not fails
        logger.warning("request reconcile: queue unavailable (%s)", exc)
        in_queue = set()
    try:
        failures = failures_by_album(await lidarr.get_history())
    except Exception as exc:  # noqa: BLE001
        logger.warning("request reconcile: history unavailable (%s)", exc)
        failures = {}

    changed = 0
    newly_failed = 0
    for request in rows:
        try:
            # A savepoint per row: a row that misbehaves rolls back only itself,
            # instead of discarding the transitions already made in this cycle.
            with db.begin_nested():
                moved = await _reconcile_one(
                    db, request, lidarr, index, in_queue, failures, now)
        except Exception as exc:  # noqa: BLE001 - one bad row is not the cycle
            logger.warning("request reconcile: request %s failed (%s)", request.id, exc)
            continue
        if moved:
            changed += 1
            if request.status == "failed":
                newly_failed += 1

    db.commit()
    _roll_up_parents(db, now)
    logger.info("requests synced: %d, changed: %d, failed: %d",
                len(rows), changed, newly_failed)
    return {"total": len(rows), "changed": changed, "failed": newly_failed}


async def _reconcile_one(db: Session, request: Request, lidarr: LidarrService,
                         index: OwnershipIndex, in_queue: set, failures: dict,
                         now: datetime) -> bool:
    album = None
    if request.release_group_mbid:
        album = await lidarr.find_album_by_release_group(request.release_group_mbid)

    evidence = library_has_release_group(
        db,
        request.release_group_mbid,
        request.artist_name or "",
        request.album_title or "",
        _track_count(album),
        index=index,
    )

    album_id = album.get("id") if album else None
    failure = failures.get(album_id) if isinstance(album_id, int) else None

    target, target_evidence, error = decide(
        request.status,
        available_evidence=evidence,
        album=album,
        in_queue=album_id in in_queue if isinstance(album_id, int) else False,
        failure=failure,
        requested_at=utc(request.requested_at),
        now=now,
    )

    request.last_synced_at = now
    if album:
        # Learn the ids if we did not have them (a request approved before they
        # were captured, or an album re-created by Lidarr's own refresh).
        request.lidarr_album_id = album_id
        if not request.lidarr_artist_id and isinstance(album.get("artistId"), int):
            request.lidarr_artist_id = album["artistId"]

    if not _may_advance(request.status, target):
        # Status unchanged; the timestamp above is the only write.
        return False

    logger.info("request %s: %s -> %s", request.id, request.status, target)
    request.status = target
    request.last_error = error
    if target == "available":
        request.available_evidence = target_evidence
    return True


def _roll_up_parents(db: Session, now: datetime) -> None:
    """Advance artist requests from their children.

    Children carry the evidence, so the parent's status is derived: all children
    available means the discography is here. Only forward moves are applied —
    the parent is a summary, never a second source of truth.
    """
    parents = (
        db.query(Request)
        .filter(Request.kind == "artist", Request.status.in_(OPEN_STATUSES))
        .all()
    )
    if not parents:
        return
    touched = False
    for parent in parents:
        children = (
            db.query(Request)
            .filter(Request.parent_request_id == parent.id)
            .all()
        )
        if not children:
            continue
        statuses = [c.status for c in children]
        if all(s == "available" for s in statuses):
            target = "available"
        elif any(s in ("downloading", "partially_available", "imported")
                 for s in statuses):
            target = "downloading"
        else:
            continue
        if _may_advance(parent.status, target):
            logger.info("artist request %s: %s -> %s (from children)",
                        parent.id, parent.status, target)
            parent.status = target
            parent.last_synced_at = now
            touched = True
    if touched:
        db.commit()


def _lidarr(db: Session) -> Optional[LidarrService]:
    """The shared client if main.py built one, else one from saved config."""
    try:
        import app.main as _main
    except Exception:  # noqa: BLE001 - importable outside the app for tests
        _main = None
    existing = getattr(_main, "lidarr_service", None)
    if existing is not None:
        return existing
    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config is None:
        return None
    return LidarrService(config.url, config.api_key)


# ── Scheduler entry point ───────────────────────────────────────────────────


async def reconcile_job() -> dict:
    """APScheduler entry: its own session, its own failure handling.

    A background cycle must never take the scheduler down, so every exception is
    logged and swallowed here rather than propagated.
    """
    db = SessionLocal()
    try:
        return await reconcile_open_requests(db)
    except Exception as exc:  # noqa: BLE001
        logger.error("request reconcile cycle failed: %s", exc)
        return {"total": 0, "changed": 0, "failed": 0, "error": str(exc)}
    finally:
        db.close()
