"""Library browsing router."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from app.models.database import get_db, Track, Album, Artist
from app.models.schemas import TrackResponse, AlbumResponse, ArtistResponse
from app.routers.auth import get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/tracks", response_model=list[TrackResponse])
async def list_tracks(
    search: Optional[str] = None,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
    genre: Optional[str] = None,
    sort: str = "title",
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List tracks with optional filtering."""
    query = db.query(Track)
    if search:
        query = query.filter(
            Track.title.ilike(f"%{search}%") |
            Track.artist_name.ilike(f"%{search}%") |
            Track.album_name.ilike(f"%{search}%")
        )
    if artist_id:
        query = query.filter(Track.artist_id == artist_id)
    if album_id:
        query = query.filter(Track.album_id == album_id)
    if genre:
        query = query.filter(Track.genre.ilike(f"%{genre}%"))

    # Sorting
    sort_map = {
        "title": Track.title,
        "artist": Track.artist_name,
        "album": Track.album_name,
        "play_count": Track.play_count.desc(),
        "rating": Track.rating.desc(),
        "recent": Track.created_at.desc().nullslast(),
    }
    query = query.order_by(sort_map.get(sort, Track.title))

    tracks = query.offset(offset).limit(limit).all()
    return [TrackResponse.model_validate(t) for t in tracks]


@router.get("/tracks/{track_id}", response_model=TrackResponse)
async def get_track(track_id: str, db: Session = Depends(get_db)):
    """Get a single track by ID."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return TrackResponse.model_validate(track)


@router.get("/albums", response_model=list[AlbumResponse])
async def list_albums(
    search: Optional[str] = None,
    sort: str = "name",
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(Album)
    if search:
        query = query.filter(
            Album.name.ilike(f"%{search}%") |
            Album.artist_name.ilike(f"%{search}%")
        )
    sort_map = {
        "name": Album.name,
        "artist": Album.artist_name,
        "year": Album.year.desc().nullslast(),
        "recent": Album.created_at.desc().nullslast(),
        "play_count": Album.play_count.desc(),
    }
    query = query.order_by(sort_map.get(sort, Album.name))
    albums = query.offset(offset).limit(limit).all()
    return [AlbumResponse.model_validate(a) for a in albums]


@router.get("/artists", response_model=list[ArtistResponse])
async def list_artists(
    search: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(Artist)
    if search:
        query = query.filter(Artist.name.ilike(f"%{search}%"))
    query = query.order_by(Artist.name)
    artists = query.offset(offset).limit(limit).all()
    return [ArtistResponse.model_validate(a) for a in artists]


@router.get("/stats")
async def library_stats(db: Session = Depends(get_db)):
    """Get library statistics."""
    return {
        "track_count": db.query(Track).count(),
        "album_count": db.query(Album).count(),
        "artist_count": db.query(Artist).count(),
    }
