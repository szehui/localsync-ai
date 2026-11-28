"""The blocklist: what the household has decided never to hold again.

One query shape, two scopes. A `release_group` row blocks one album; an `artist`
row blocks everything by that artist. Both are keyed by MusicBrainz ID, because a
block matched by title would silently cover a different album with the same name
— the exact failure the request path is built to avoid.

The rules this module exists to keep in one place:

* a block is **enforced at creation**, not merely hidden in the UI. A client that
  never saw a filtered search can still post an MBID, and the answer must be the
  same: refused.
* blocked items are **filtered from catalog results**, so the household is not
  offered something it has already rejected. In artist detail they are kept but
  flagged, because an album silently missing from a discography reads as a bug.
* a block **does not cancel an existing request**. Blocking is forward-looking;
  killing work in flight is a decision the owner should make explicitly.
"""
import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.database import Blocklist, User

logger = logging.getLogger(__name__)

SCOPES = ("release_group", "artist")


class BlocklistError(Exception):
    """A block that must be refused, with the reason the UI should show."""

    def __init__(self, message: str, *, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def blocked_rows(db: Session, *, release_group_mbid: Optional[str] = None,
                 artist_mbid_: Optional[str] = None) -> Optional[Blocklist]:
    """The row that blocks this album or artist, if any."""
    conditions = []
    if release_group_mbid:
        conditions.append(
            (Blocklist.scope == "release_group")
            & (Blocklist.release_group_mbid == release_group_mbid))
    if artist_mbid_:
        conditions.append(
            (Blocklist.scope == "artist")
            & (Blocklist.artist_mbid == artist_mbid_))
    if not conditions:
        return None
    expr = conditions[0]
    for extra in conditions[1:]:
        expr = expr | extra
    return db.query(Blocklist).filter(expr).first()


def blocked_sets(db: Session) -> tuple[set[str], set[str]]:
    """Every blocked release group and artist, in one query each.

    Catalog pages annotate dozens of release groups at a time; asking per card
    would be one query per album for a fact that fits in two sets.
    """
    rows = db.query(Blocklist.scope, Blocklist.release_group_mbid,
                    Blocklist.artist_mbid).all()
    release_groups: set[str] = set()
    artists: set[str] = set()
    for scope, rg, artist in rows:
        if scope == "release_group" and rg:
            release_groups.add(rg)
        elif scope == "artist" and artist:
            artists.add(artist)
    return release_groups, artists


def filter_blocked(db: Session, cards: Iterable[dict]) -> list[dict]:
    """Drop cards the blocklist refuses, matching either scope."""
    release_groups, artists = blocked_sets(db)
    if not release_groups and not artists:
        return list(cards)
    return [
        card for card in cards
        if card.get("release_group_mbid") not in release_groups
        and card.get("artist_mbid") not in artists
    ]


def _display(db: Session, user_id: Optional[int]) -> Optional[str]:
    if user_id is None:
        return None
    row = db.get(User, user_id)
    return (row.display_name or row.username) if row else None


def to_entry(db: Session, row: Blocklist) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "release_group_mbid": row.release_group_mbid,
        "artist_mbid": row.artist_mbid,
        "artist_name": row.artist_name,
        "album_title": row.album_title,
        "reason": row.reason,
        "created_by": _display(db, row.created_by),
        "created_at": row.created_at,
    }


def list_entries(db: Session, *, scope: Optional[str] = None) -> list[dict]:
    query = db.query(Blocklist)
    if scope in SCOPES:
        query = query.filter(Blocklist.scope == scope)
    rows = query.order_by(Blocklist.created_at.desc(), Blocklist.id.desc()).all()
    return [to_entry(db, row) for row in rows]


def add(db: Session, actor, *, scope: str = "release_group",
        release_group_mbid: Optional[str] = None,
        artist_mbid: Optional[str] = None,
        artist_name: Optional[str] = None,
        album_title: Optional[str] = None,
        reason: Optional[str] = None) -> Blocklist:
    """Create a block. Refuses a duplicate instead of stacking rows."""
    if scope not in SCOPES:
        raise BlocklistError(f"Unknown blocklist scope {scope!r}", code="bad_scope")

    uid = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    row_user = db.get(User, uid) if uid is not None else None
    if row_user is None:
        raise BlocklistError("Unknown user", code="unknown_user", status=401)
    if not row_user.is_owner:
        raise BlocklistError("Only the owner can change the blocklist",
                             code="forbidden", status=403)

    release_group_mbid = (release_group_mbid or "").strip() or None
    target_artist = (artist_mbid or "").strip() or None

    if scope == "release_group":
        if not release_group_mbid:
            raise BlocklistError("release_group_mbid is required for an album block",
                                 code="missing_mbid")
        if blocked_rows(db, release_group_mbid=release_group_mbid):
            raise BlocklistError("That album is already blocked",
                                 code="duplicate", status=409)
    else:
        if not target_artist:
            raise BlocklistError("artist_mbid is required for an artist block",
                                 code="missing_mbid")
        if blocked_rows(db, artist_mbid_=target_artist):
            raise BlocklistError("That artist is already blocked",
                                 code="duplicate", status=409)

    row = Blocklist(
        scope=scope,
        release_group_mbid=release_group_mbid if scope == "release_group" else None,
        artist_mbid=target_artist if scope == "artist" else None,
        artist_name=(artist_name or "").strip() or None,
        album_title=(album_title or "").strip() or None,
        reason=(reason or "").strip() or None,
        created_by=row_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("blocklist: %s %s blocked by %s", scope,
                release_group_mbid or target_artist, row_user.username)
    return row


def remove(db: Session, actor, entry_id: int) -> Blocklist:
    """Delete a block. Owner-only, and 404 for a row that is not there."""
    uid = actor.get("id") if isinstance(actor, dict) else getattr(actor, "id", None)
    row_user = db.get(User, uid) if uid is not None else None
    if row_user is None:
        raise BlocklistError("Unknown user", code="unknown_user", status=401)
    if not row_user.is_owner:
        raise BlocklistError("Only the owner can change the blocklist",
                             code="forbidden", status=403)

    row = db.get(Blocklist, entry_id)
    if row is None:
        raise BlocklistError("Blocklist entry not found", code="not_found",
                             status=404)
    db.delete(row)
    db.commit()
    return row
