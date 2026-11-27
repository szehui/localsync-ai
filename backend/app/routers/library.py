"""Library browsing router."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session
from typing import Optional
from app.models.database import get_db, Track, TrackAnnotation, Album, Artist
from app.models.schemas import TrackResponse, AlbumResponse, ArtistResponse
from app.routers.auth import get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


def _annotated_track_query(db: Session, user_id: int):
    """Track rows left-joined to the caller's annotations.

    Play counts, ratings and stars are per Subsonic account and now live in
    `track_annotations`, not on `tracks`. A LEFT join keeps never-played tracks
    in the listing (they are the majority of a library) while still letting the
    caller sort by their own listening.
    """
    return (
        db.query(
            Track,
            func.coalesce(TrackAnnotation.play_count, 0).label("ann_play_count"),
            func.coalesce(TrackAnnotation.rating, 0).label("ann_rating"),
            func.coalesce(TrackAnnotation.starred, False).label("ann_starred"),
        )
        .outerjoin(
            TrackAnnotation,
            and_(
                TrackAnnotation.track_id == Track.id,
                TrackAnnotation.user_id == user_id,
            ),
        )
    )


def _track_response(track: Track, play_count, rating, starred) -> TrackResponse:
    return TrackResponse(
        id=track.id,
        title=track.title,
        album_id=track.album_id,
        album_name=track.album_name,
        artist_id=track.artist_id,
        artist_name=track.artist_name,
        genre=track.genre,
        year=track.year,
        duration=track.duration,
        play_count=play_count or 0,
        rating=rating or 0,
        starred=bool(starred),
        created_at=track.created_at,
    )


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
    user: dict = Depends(get_current_user),
):
    """List tracks with optional filtering, annotated for the calling user."""
    query = _annotated_track_query(db, user["id"])
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

    # Sorting. The play/rating keys sort on the caller's annotations — sorting
    # on the legacy `tracks` columns would return arbitrary order, since sync
    # no longer writes them.
    sort_map = {
        "title": Track.title,
        "artist": Track.artist_name,
        "album": Track.album_name,
        "play_count": func.coalesce(TrackAnnotation.play_count, 0).desc(),
        "rating": func.coalesce(TrackAnnotation.rating, 0).desc(),
        "recent": Track.created_at.desc().nullslast(),
    }
    query = query.order_by(sort_map.get(sort, Track.title))

    rows = query.offset(offset).limit(limit).all()
    return [_track_response(t, pc, rt, st) for t, pc, rt, st in rows]


@router.get("/tracks/{track_id}", response_model=TrackResponse)
async def get_track(
    track_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Get a single track by ID, annotated for the calling user."""
    row = _annotated_track_query(db, user["id"]).filter(Track.id == track_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Track not found")
    track, play_count, rating, starred = row
    return _track_response(track, play_count, rating, starred)



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
