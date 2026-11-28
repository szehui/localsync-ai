"""Blocklist API — owner-only.

Every route requires the owner: a blocklist is a household decision, and a member
who could write to it could silently remove albums from everyone's search
results. Reading is owner-only too, because the reasons are the owner's notes.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.database import User, get_db
from app.models.schemas import BlocklistCreate, BlocklistEntry, BlocklistListResponse
from app.routers.auth import get_current_user
from app.services import blocklist as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blocklist", tags=["blocklist"])


def _error(exc: svc.BlocklistError) -> HTTPException:
    return HTTPException(status_code=exc.status,
                         detail={"code": exc.code, "message": exc.message})


@router.get("", response_model=BlocklistListResponse)
async def list_blocklist(
    scope: Optional[str] = Query(None, pattern="^(release_group|artist)$"),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Everything blocked, newest first. Owner-only: the reasons are notes."""
    caller = db.get(User, user["id"])
    if caller is None:
        raise HTTPException(status_code=401, detail="Unknown user")
    if not caller.is_owner:
        raise HTTPException(status_code=403,
                            detail="Only the owner can see the blocklist")
    entries = svc.list_entries(db, scope=scope)
    return BlocklistListResponse(entries=[BlocklistEntry(**e) for e in entries],
                                 total=len(entries))


@router.post("", status_code=201)
async def add_block(
    payload: BlocklistCreate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = svc.add(
            db, user,
            scope=payload.scope,
            release_group_mbid=payload.release_group_mbid,
            artist_mbid=payload.artist_mbid,
            artist_name=payload.artist_name,
            album_title=payload.album_title,
            reason=payload.reason,
        )
    except svc.BlocklistError as exc:
        raise _error(exc)
    return svc.to_entry(db, row)


@router.delete("/{entry_id}")
async def remove_block(
    entry_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = svc.remove(db, user, entry_id)
    except svc.BlocklistError as exc:
        raise _error(exc)
    return {"removed": True, "id": row.id}
