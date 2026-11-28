"""Cache for upstream catalog responses.

MusicBrainz allows roughly one request per second. A search box that re-queried
on every keystroke, or a landing page that re-fetched rails per view, would spend
the entire budget on one person. This table is the difference between a usable UI
and a 503.

TTLs are chosen per kind rather than globally because the data ages very
differently: a release group's existence and track list effectively never change,
while "new releases this fortnight" is stale within the hour.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.database import CatalogCache

logger = logging.getLogger(__name__)

TTL_SECONDS = {
    "search": 600,        # 10 min — typeahead, but cheap to redo
    "album": 604800,      # 7 days — a release group's metadata is stable
    "artist": 86400,      # 1 day
    "rails": 3600,        # 1 hour — "fresh releases" moves daily at most
    "coverart": 604800,   # 7 days, and negatives are cached too
}

DEFAULT_TTL = 3600


def make_key(kind: str, *parts: Any) -> str:
    """Build a stable key, e.g. ``search:daft punk:0``."""
    pieces = [kind] + [str(p).strip().lower() for p in parts if p is not None]
    return ":".join(pieces)


def _ttl(kind: str) -> int:
    return TTL_SECONDS.get(kind, DEFAULT_TTL)


def get(db: Session, key: str, kind: str) -> Optional[Any]:
    """Return the cached payload, or None when absent or stale.

    A stale row is deleted on read, so the table does not grow forever in a
    deployment where nobody prunes it.
    """
    row = db.query(CatalogCache).filter(CatalogCache.key == key).first()
    if row is None:
        return None

    age = datetime.utcnow() - (row.fetched_at or datetime.utcnow())
    if age > timedelta(seconds=_ttl(kind)):
        db.delete(row)
        db.commit()
        return None

    try:
        return json.loads(row.payload)
    except (TypeError, ValueError):
        # Corrupt payload: drop it rather than serving garbage upstream.
        logger.warning("Discarding unreadable cache payload for %s", key)
        db.delete(row)
        db.commit()
        return None


def put(db: Session, key: str, kind: str, payload: Any) -> None:
    """Store a payload, replacing any existing row for that key."""
    row = db.query(CatalogCache).filter(CatalogCache.key == key).first()
    encoded = json.dumps(payload, ensure_ascii=False)
    if row is None:
        db.add(CatalogCache(key=key, kind=kind, payload=encoded,
                            fetched_at=datetime.utcnow()))
    else:
        row.kind = kind
        row.payload = encoded
        row.fetched_at = datetime.utcnow()
    db.commit()


def clear(db: Session, kind: str | None = None) -> int:
    """Delete cached rows, optionally only one kind. Returns the count removed."""
    query = db.query(CatalogCache)
    if kind is not None:
        query = query.filter(CatalogCache.kind == kind)
    removed = query.count()
    query.delete(synchronize_session=False)
    db.commit()
    return removed
