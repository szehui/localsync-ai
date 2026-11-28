"""Catalog endpoints: search, album/artist detail, rails.

Every route requires a signed-in user. Upstream hiccups surface as 503 with a
retryable message — the client can show that, and it must not be treated as "no
results", which would read as truth.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.routers.auth import get_current_user
from app.services import catalog
from app.services.musicbrainz import MusicBrainzUnavailable

router = APIRouter(prefix="/api/catalog", tags=["catalog"],
                   dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


@router.get("/search")
async def search(
    q: str = Query("", description="album or artist text"),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Search the catalog. Empty queries are rejected locally — a blank search
    box must never spend a MusicBrainz request."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="empty query")
    try:
        return await catalog.search(db, user, q, limit=limit, offset=offset)
    except MusicBrainzUnavailable:
        raise HTTPException(
            status_code=503, detail="MusicBrainz is busy; retry in a few seconds")


@router.get("/rails")
async def get_rails(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    try:
        return await catalog.rails(db, user)
    except MusicBrainzUnavailable:
        raise HTTPException(
            status_code=503, detail="MusicBrainz is busy; retry in a few seconds")


@router.get("/album/{release_group_mbid}")
async def album(
    release_group_mbid: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    try:
        detail = await catalog.album_detail(db, user, release_group_mbid)
    except MusicBrainzUnavailable:
        raise HTTPException(
            status_code=503, detail="MusicBrainz is busy; retry in a few seconds")
    if detail is None:
        raise HTTPException(status_code=404, detail="album not found")
    return detail


@router.get("/artist/{artist_mbid}")
async def artist(
    artist_mbid: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    try:
        detail = await catalog.artist_detail(db, user, artist_mbid)
    except MusicBrainzUnavailable:
        raise HTTPException(
            status_code=503, detail="MusicBrainz is busy; retry in a few seconds")
    if detail is None:
        raise HTTPException(status_code=404, detail="artist not found")
    return detail
