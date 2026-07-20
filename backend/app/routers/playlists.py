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
    PlaylistFromPlaylistRequest,
    NavidromePlaylist,
    TrackResponse,
)
from app.routers.auth import get_navidrome_client, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


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
    track_ids = []  # will hold other track IDs (excluding seed)
    target_other = max(0, request.track_count - 1)  # we want this many other tracks
    for song_data in similar:
        track_id = song_data["id"]
        if track_id == request.seed_track_id:
            continue
        if len(track_ids) >= target_other:
            break
        # Look up in local cache for filtering
        cached = db.query(Track).filter(Track.id == track_id).first()
        if cached and _passes_strictness(cached, seed_track, request.strictness):
            track_ids.append(track_id)

    # If strictness filter was too aggressive, relax and fill from similar (still excluding seed)
    if len(track_ids) < target_other:
        for song_data in similar:
            track_id = song_data["id"]
            if track_id == request.seed_track_id:
                continue
            if len(track_ids) >= target_other:
                break
            if track_id not in track_ids:  # avoid duplicates
                track_ids.append(track_id)

    # Prepend the seed track to the list
    final_track_ids = [request.seed_track_id] + track_ids
    # Fetch full track data for the final list
    tracks = db.query(Track).filter(Track.id.in_(final_track_ids)).all()
    track_map = {t.id: t for t in tracks}
    ordered_tracks = [track_map[tid] for tid in final_track_ids if tid in track_map]

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


@router.get("/{playlist_id}/tracks", response_model=list[TrackResponse])
async def get_playlist_tracks(
    playlist_id: int,
    db: Session = Depends(get_db),
):
    """Get the full track listing for a generated playlist."""
    playlist = db.query(GeneratedPlaylist).filter(GeneratedPlaylist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if not playlist.track_ids:
        return []
    try:
        track_ids = json.loads(playlist.track_ids)
    except (json.JSONDecodeError, TypeError):
        return []
    tracks = db.query(Track).filter(Track.id.in_(track_ids)).all()
    track_map = {t.id: t for t in tracks}
    ordered_tracks = [track_map[tid] for tid in track_ids if tid in track_map]
    return [TrackResponse.model_validate(t) for t in ordered_tracks]


@router.get("/navidrome", response_model=list[NavidromePlaylist])
async def list_navidrome_playlists(client=Depends(get_navidrome_client)):
    """List all playlists on the Navidrome server."""
    if client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")
    try:
        playlists = await client.get_playlists()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Navidrome error: {e}")
    return [
        NavidromePlaylist(
            id=p["id"],
            name=p.get("name", "Unnamed"),
            song_count=p.get("songCount", 0),
            owner=p.get("owner", ""),
            public=p.get("public", False),
            created=p.get("created"),
            cover_art=p.get("coverArt"),
        )
        for p in playlists
    ]


@router.post("/generate-from-playlist", response_model=PlaylistGenerateResponse)
async def generate_from_playlist(
    request: PlaylistFromPlaylistRequest,
    db: Session = Depends(get_db),
    client=Depends(get_navidrome_client),
):
    """Generate a playlist from a seed playlist using Navidrome similarity."""
    if client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    # Fetch the seed playlist from Navidrome
    try:
        nav_playlist = await client.get_playlist(request.navidrome_playlist_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Navidrome fetch error: {e}")

    entries = nav_playlist.get("playlist", {}).get("entry", [])
    if not entries:
        raise HTTPException(status_code=404, detail="Seed playlist has no tracks")

    seed_track_ids = [s["id"] for s in entries]
    source_name = nav_playlist.get("playlist", {}).get("name", "Unknown Playlist")

    # For each seed track, get similar songs
    all_candidates: list[str] = []
    seen = set(seed_track_ids)  # avoid adding seed tracks to candidates
    # Pre-seed the ordered list with the original playlist tracks
    ordered_seed_ids: list[str] = []
    for s in entries:
        tid = s["id"]
        if tid not in seen:
            seen.add(tid)
            ordered_seed_ids.append(tid)

    for seed_id in seed_track_ids:
        try:
            similar = await client.get_similar_songs2(seed_id, count=request.per_seed_track)
            for song_data in similar:
                tid = song_data["id"]
                if tid not in seen:
                    seen.add(tid)
                    all_candidates.append(tid)
        except Exception:
            continue  # skip if similarity fails for one track

    # Apply strictness filter using cached tracks
    filtered_tracks: list[str] = []
    for tid in all_candidates:
        if len(filtered_tracks) >= request.track_count:
            break
        cached = db.query(Track).filter(Track.id == tid).first()
        # Use the first seed track as reference for strictness filtering
        if cached and seed_track_ids:
            first_seed = db.query(Track).filter(Track.id == seed_track_ids[0]).first()
            if first_seed and _passes_strictness(cached, first_seed, request.strictness):
                filtered_tracks.append(tid)
            elif first_seed is None:
                filtered_tracks.append(tid)  # no reference track in cache, include anyway
        else:
            filtered_tracks.append(tid)

    # If strictness was too aggressive, relax with remaining candidates
    if len(filtered_tracks) < request.track_count:
        for tid in all_candidates:
            if len(filtered_tracks) >= request.track_count:
                break
            if tid not in filtered_tracks:
                filtered_tracks.append(tid)

    # Final track list: seed playlist tracks (up to 10) + similar tracks
    from itertools import islice
    final_track_ids = list(islice(ordered_seed_ids, 10)) + filtered_tracks
    final_track_ids = final_track_ids[:request.track_count]

    # Fetch full track data
    tracks = db.query(Track).filter(Track.id.in_(final_track_ids)).all()
    track_map = {t.id: t for t in tracks}
    ordered_tracks = [track_map[tid] for tid in final_track_ids if tid in track_map]

    playlist_name = f"Inspired By: {source_name}"

    # Save to local DB
    playlist = GeneratedPlaylist(
        name=playlist_name,
        seed_track_id=seed_track_ids[0] if seed_track_ids else None,
        seed_track_name=f"Playlist: {source_name}",
        seed_playlist_id=request.navidrome_playlist_id,
        seed_playlist_name=source_name,
        strictness=request.strictness,
        track_count=len(ordered_tracks),
        track_ids=json.dumps(final_track_ids),
    )
    db.add(playlist)
    db.commit()
    db.refresh(playlist)

    return PlaylistGenerateResponse(
        name=playlist_name,
        tracks=[TrackResponse.model_validate(t) for t in ordered_tracks],
        track_count=len(ordered_tracks),
    )


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
