"""Playlist generation and management router."""
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db, Track, GeneratedPlaylist
from app.models.schemas import (
    PlaylistGenerateRequest,
    PlaylistGenerateResponse,
    PlaylistPushRequest,
    PlaylistPushResponse,
    TrackResponse,
)
from app.routers.auth import get_navidrome_client

router = APIRouter()


@router.post("/generate", response_model=PlaylistGenerateResponse)
async def generate_playlist(
    request: PlaylistGenerateRequest,
    db: Session = Depends(get_db),
    client=Depends(get_navidrome_client),
):
    """Generate a playlist from a seed track using Navidrome similarity."""
    if client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    # Validate seed track exists in cache
    seed_track = db.query(Track).filter(Track.id == request.seed_track_id).first()
    if not seed_track:
        raise HTTPException(status_code=404, detail="Seed track not found in library")

    # Get similar songs from Navidrome
    try:
        similar = await client.get_similar_songs2(
            request.seed_track_id, count=request.track_count * 2
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Navidrome similarity error: {e}")

    if not similar:
        raise HTTPException(status_code=404, detail="No similar tracks found")

    # Build track list from cache, applying strictness filter
    track_ids = []
    for song_data in similar:
        track_id = song_data["id"]
        if track_id == request.seed_track_id:
            continue

        # Look up in local cache for filtering
        cached = db.query(Track).filter(Track.id == track_id).first()
        if cached and _passes_strictness(cached, seed_track, request.strictness):
            track_ids.append(track_id)

        if len(track_ids) >= request.track_count:
            break

    # If strictness filter was too aggressive, relax and fill from similar
    if len(track_ids) < request.track_count:
        for song_data in similar:
            track_id = song_data["id"]
            if track_id not in track_ids and track_id != request.seed_track_id:
                track_ids.append(track_id)
            if len(track_ids) >= request.track_count:
                break

    # Fetch full track data
    tracks = db.query(Track).filter(Track.id.in_(track_ids)).all()
    track_map = {t.id: t for t in tracks}
    ordered_tracks = [track_map[tid] for tid in track_ids if tid in track_map]

    # Generate playlist name
    playlist_name = f"More Like This: {seed_track.title}"

    # Save to local DB
    playlist = GeneratedPlaylist(
        name=playlist_name,
        seed_track_id=request.seed_track_id,
        seed_track_name=seed_track.title,
        strictness=request.strictness,
        track_count=len(ordered_tracks),
        track_ids=json.dumps(track_ids),
    )
    db.add(playlist)
    db.commit()
    db.refresh(playlist)

    return PlaylistGenerateResponse(
        name=playlist_name,
        tracks=[TrackResponse.model_validate(t) for t in ordered_tracks],
        track_count=len(ordered_tracks),
    )


@router.post("/push", response_model=PlaylistPushResponse)
async def push_playlist(
    request: PlaylistPushRequest,
    db: Session = Depends(get_db),
    client=Depends(get_navidrome_client),
):
    """Push a playlist to Navidrome (create new or update existing)."""
    if client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    # Check if playlist with this name already exists
    existing = db.query(GeneratedPlaylist).filter(
        GeneratedPlaylist.name == request.name
    ).first()

    try:
        if existing and existing.navidrome_playlist_id:
            # Update in-place
            result = await client.update_playlist(
                existing.navidrome_playlist_id,
                request.track_ids,
                name=request.name,
            )
            playlist_id = existing.navidrome_playlist_id
        else:
            # Create new
            result = await client.create_playlist(request.name, request.track_ids)
            playlist_id = result.get("playlist", {}).get("id", "")

        # Update local record
        if existing:
            existing.navidrome_playlist_id = playlist_id
            existing.track_ids = json.dumps(request.track_ids)
            existing.track_count = len(request.track_ids)
            db.commit()

        return PlaylistPushResponse(
            playlist_id=playlist_id,
            name=request.name,
            track_count=len(request.track_ids),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Navidrome playlist error: {e}")


@router.get("/")
async def list_generated_playlists(db: Session = Depends(get_db)):
    """List all locally generated playlists."""
    playlists = db.query(GeneratedPlaylist).order_by(
        GeneratedPlaylist.updated_at.desc()
    ).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "navidrome_playlist_id": p.navidrome_playlist_id,
            "seed_track_name": p.seed_track_name,
            "strictness": p.strictness,
            "track_count": p.track_count,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in playlists
    ]


def _passes_strictness(track: Track, seed: Track, strictness: int) -> bool:
    """Filter tracks based on strictness level (1=loose, 5=strict)."""
    if strictness <= 1:
        return True  # No filtering
    if strictness >= 5:
        # Strict: same artist AND same genre
        return bool(
            track.artist_id == seed.artist_id
            and track.genre
            and seed.genre
            and track.genre.lower() == seed.genre.lower()
        )
    if strictness >= 4:
        # High: same genre
        return bool(
            track.genre
            and seed.genre
            and track.genre.lower() == seed.genre.lower()
        )
    if strictness >= 3:
        # Medium: same artist OR same genre
        same_artist = track.artist_id == seed.artist_id
        same_genre = (
            track.genre
            and seed.genre
            and track.genre.lower() == seed.genre.lower()
        )
        return bool(same_artist or same_genre)
    # Low (2): same artist OR same album
    return bool(track.artist_id == seed.artist_id or track.album_id == seed.album_id)
