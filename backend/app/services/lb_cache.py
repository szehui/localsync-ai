"""TTL caches for ListenBrainz responses.

Both ListenBrainz similarity paths are call-expensive relative to the 30/window
budget, and neither changes quickly:

* CF recommendations are regenerated upstream on a weekly cadence -> 24h TTL.
* Labs similar-recordings takes exactly ONE MBID per call, so a 20-seed run
  costs 20 calls unless results are cached -> 7d TTL.

Empty results are cached deliberately. `[]` from Labs means "this dataset has no
neighbours for that recording", which is a stable fact worth remembering — not a
transient failure to retry.
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.database import LbRecommendationCache, LbSimilarCache

logger = logging.getLogger(__name__)

RECOMMENDATION_TTL = timedelta(hours=24)
SIMILAR_TTL = timedelta(days=7)


def _is_stale(fetched_at: datetime | None, ttl: timedelta,
              now: datetime | None = None) -> bool:
    if fetched_at is None:
        return True
    reference = now or datetime.utcnow()
    return (reference - fetched_at) > ttl


def _load_json(raw: str, default):
    """Decode cached JSON, degrading to `default` on corruption."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ListenBrainz cache: discarding malformed JSON payload")
        return default


# ── recommendations ──────────────────────────────────────────────────────────

def get_cached_recommendations(db: Session,
                               now: datetime | None = None) -> list[dict] | None:
    """Cached CF recommendations, or None on miss/stale."""
    row = db.query(LbRecommendationCache).filter(
        LbRecommendationCache.id == 1).first()
    if row is None or _is_stale(row.fetched_at, RECOMMENDATION_TTL, now):
        return None
    return _load_json(row.mbids, [])


def put_recommendations(db: Session, mbids: list[dict],
                        now: datetime | None = None) -> None:
    """Store CF recommendations, replacing any existing row."""
    payload = json.dumps(mbids)
    fetched_at = now or datetime.utcnow()
    row = db.query(LbRecommendationCache).filter(
        LbRecommendationCache.id == 1).first()
    if row is None:
        db.add(LbRecommendationCache(id=1, mbids=payload, fetched_at=fetched_at))
    else:
        row.mbids = payload
        row.fetched_at = fetched_at
    db.commit()


# ── similar recordings ───────────────────────────────────────────────────────

def get_cached_similar(db: Session, recording_mbid: str,
                       now: datetime | None = None) -> list[dict] | None:
    """Cached Labs similar-recordings for one seed, or None on miss/stale."""
    if not recording_mbid:
        return None
    row = db.query(LbSimilarCache).filter(
        LbSimilarCache.recording_mbid == recording_mbid).first()
    if row is None or _is_stale(row.fetched_at, SIMILAR_TTL, now):
        return None
    return _load_json(row.payload, [])


def put_similar(db: Session, recording_mbid: str, payload: list[dict],
                now: datetime | None = None) -> None:
    """Store Labs similar-recordings for one seed, replacing any existing row."""
    if not recording_mbid:
        return
    encoded = json.dumps(payload)
    fetched_at = now or datetime.utcnow()
    row = db.query(LbSimilarCache).filter(
        LbSimilarCache.recording_mbid == recording_mbid).first()
    if row is None:
        db.add(LbSimilarCache(recording_mbid=recording_mbid, payload=encoded,
                              fetched_at=fetched_at))
    else:
        row.payload = encoded
        row.fetched_at = fetched_at
    db.commit()
