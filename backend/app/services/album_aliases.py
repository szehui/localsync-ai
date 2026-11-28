"""Alternate release-group titles (the K-pop problem, solved locally).

A release group's releases carry localised titles. ``싸다9`` contains three
official releases, one of them titled ``PSY 9th``, so the English name a user
remembers is already in MusicBrainz — it just is not the group's title.

Harvesting those titles on first sight gives two things:

* a display label, so a card can read ``싸다9 — PSY 9th`` and be recognised
* an instant local match, so typing "psy 9th" does not need an upstream call

Aliases are a property of the release group, not of a user or a library row.
"""
import logging
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.database import AlbumAlias
from app.services.musicbrainz import also_known_as
from app.services.title_norm import fold

logger = logging.getLogger(__name__)


def record_aliases(db: Session, release_group_mbid: str, detail: dict) -> list[str]:
    """Persist alternate titles from a release-group detail payload.

    Idempotent: existing aliases are left alone, so this is safe on every detail
    view and on every backfill pass.
    """
    if not release_group_mbid:
        return []

    titles = also_known_as(detail)
    if not titles:
        return []

    existing = {
        row.alias for row in db.query(AlbumAlias)
        .filter(AlbumAlias.release_group_mbid == release_group_mbid).all()
    }
    added = []
    for title in titles:
        if title in existing:
            continue
        db.add(AlbumAlias(release_group_mbid=release_group_mbid, alias=title,
                          source="release_title"))
        added.append(title)
    if added:
        db.commit()
    return added


def aliases_for(db: Session, release_group_mbid: str) -> list[str]:
    rows = (db.query(AlbumAlias)
            .filter(AlbumAlias.release_group_mbid == release_group_mbid)
            .all())
    return sorted({row.alias for row in rows})


def match(db: Session, query: str, *, limit: int = 5) -> dict[str, str]:
    """Find release groups whose alias contains the query, folded.

    Returns ``{release_group_mbid: alias}``. Deliberately a local, exact-folded
    substring test — no scoring and no fuzzy distance, because a wrong local hit
    would show a card for an album the user did not mean.
    """
    needle = fold(query)
    if not needle or len(needle) < 3:
        return {}

    out: dict[str, str] = {}
    for row in db.query(AlbumAlias).all():
        if needle in fold(row.alias):
            out.setdefault(row.release_group_mbid, row.alias)
            if len(out) >= limit:
                break
    return out


def harvest(db: Session, entries: Iterable[tuple[str, dict]]) -> int:
    """Record aliases for many release groups at once. Returns the count added."""
    added = 0
    for mbid, detail in entries:
        added += len(record_aliases(db, mbid, detail))
    return added
