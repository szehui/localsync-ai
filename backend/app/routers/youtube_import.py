"""YouTube Music import router."""
import re
import logging
from typing import Dict, List, Any, Optional
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.schemas_youtube import (
    YouTubeParseResponse,
    YouTubeImportRequest,
    YouTubeImportResult,
    TrackMatchResult,
    LidarrConfigRequest,
    LidarrConfigResponse,
)
from app.models.database import LidarrConfig
from app.services.youtube_music import youtube_music_service
from app.services.lidarr import LidarrService
from app.services.navidrome import NavidromeClient
from app.routers.auth import get_navidrome_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

# Common connector/stop words to strip when comparing music metadata
_STOP_WORDS = frozenset({"the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "is"})


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation, remove extra spaces."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> set[str]:
    """Normalize, split into tokens, drop stop words."""
    return set(_normalize_text(text).split()) - _STOP_WORDS


def _match_score(title1: str, artist1: str, title2: str, artist2: str) -> float:
    """
    Calculate match score between two tracks.
    Uses a blended strategy:
      - SequenceMatcher on normalized strings (handles typos, reordering)
      - Token overlap ratio (handles dropped/added words like "the", "and")
    Returns a score between 0 and 1 (1 = perfect match).
    """
    norm_t1 = _normalize_text(title1)
    norm_t2 = _normalize_text(title2)
    norm_a1 = _normalize_text(artist1)
    norm_a2 = _normalize_text(artist2)

    # SequenceMatcher component (character-level)
    seq_title = SequenceMatcher(None, norm_t1, norm_t2).ratio()
    seq_artist = SequenceMatcher(None, norm_a1, norm_a2).ratio()
    seq_score = (seq_title * 0.7) + (seq_artist * 0.3)

    # Token overlap component (word-level, stop-word blind)
    tok_t1 = _tokenize(title1)
    tok_t2 = _tokenize(title2)
    tok_a1 = _tokenize(artist1)
    tok_a2 = _tokenize(artist2)

    def _token_ratio(a: set[str], b: set[str]) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    tok_title = _token_ratio(tok_t1, tok_t2)
    tok_artist = _token_ratio(tok_a1, tok_a2)
    tok_score = (tok_title * 0.7) + (tok_artist * 0.3)

    # Take the better of the two strategies
    return max(seq_score, tok_score)


def _get_lidarr_service(db: Session) -> Optional[LidarrService]:
    """Get Lidarr service from database config."""
    # Use module-level reference so updates via set_lidarr_service() are visible
    import app.main as _main

    if _main.lidarr_service is not None:
        return _main.lidarr_service

    # Try to load from database
    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config:
        _main.set_lidarr_service(LidarrService(config.url, config.api_key))
        return _main.lidarr_service

    return None


@router.post("/parse", response_model=YouTubeParseResponse)
async def parse_youtube_playlist(request: YouTubeImportRequest):
    """Parse a YouTube Music playlist URL and return track information."""
    try:
        playlist_data = await youtube_music_service.get_playlist_tracks(request.url)
        return YouTubeParseResponse(
            playlist_title=playlist_data["title"],
            track_count=playlist_data["track_count"],
            tracks=playlist_data["tracks"],
        )
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to parse YouTube playlist: {e}"
        )


@router.post("/import", response_model=YouTubeImportResult)
async def import_youtube_playlist(
    request: YouTubeImportRequest,
    db: Session = Depends(get_db),
    client: Optional[NavidromeClient] = Depends(get_navidrome_client),
):
    """
    Import a YouTube Music playlist to Navidrome, with missing tracks queued to Lidarr.

    If a playlist with the same name already exists in Navidrome, it is
    updated in place (tracks replaced) rather than creating a duplicate.
    """
    if client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    lidarr = _get_lidarr_service(db)

    # Step 1: Parse YouTube Music playlist
    try:
        playlist_data = await youtube_music_service.get_playlist_tracks(request.url)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to parse YouTube playlist: {e}"
        )

    playlist_name = request.playlist_name or playlist_data["title"]

    # Step 2: Search Navidrome for each track
    matched_track_ids: List[str] = []
    track_results: List[TrackMatchResult] = []
    missing_artists: Dict[str, Dict[str, Any]] = {}

    for track in playlist_data["tracks"]:
        try:
            # Search strategy: title-only first (most flexible), then
            # combined, then artist-only. Verify artist match via fuzzy scoring.
            songs: List[dict] = []

            # Strategy 1: title-only (best for artist name variations)
            if track["title"]:
                title_result = await client.search(
                    query=track["title"],
                    artist_count=0,
                    album_count=0,
                    song_count=20,
                )
                songs = title_result.get("searchResult3", {}).get("song", [])

            # Strategy 2: combined artist+title
            if not songs and track["artist"] and track["title"]:
                combo_result = await client.search(
                    query=f"{track['artist']} {track['title']}",
                    artist_count=0,
                    album_count=0,
                    song_count=10,
                )
                songs = combo_result.get("searchResult3", {}).get("song", [])

            # Strategy 3: artist-only (broad, last resort)
            if not songs and track["artist"]:
                artist_result = await client.search(
                    query=track["artist"],
                    artist_count=5,
                    album_count=0,
                    song_count=20,
                )
                songs = artist_result.get("searchResult3", {}).get("song", [])

            # Find best match across all candidates
            best_match = None
            best_score = 0.0

            for song in songs:
                score = _match_score(
                    track["title"],
                    track["artist"],
                    song.get("title", ""),
                    song.get("artist", ""),
                )
                if score > best_score and score >= 0.75:
                    best_score = score
                    best_match = song

            if best_match:
                matched_track_ids.append(best_match["id"])
                track_results.append(
                    TrackMatchResult(
                        title=track["title"],
                        artist=track["artist"],
                        album=track["album"],
                        status="matched",
                    )
                )
            else:
                artist_name = track["artist"]
                if artist_name not in missing_artists:
                    missing_artists[artist_name] = {
                        "artist_name": artist_name,
                        "album_name": track["album"],
                    }
                track_results.append(
                    TrackMatchResult(
                        title=track["title"],
                        artist=track["artist"],
                        album=track["album"],
                        status="missing",
                    )
                )

        except Exception as e:
            logger.warning(f"Search failed for {track['artist']} - {track['title']}: {e}")
            track_results.append(
                TrackMatchResult(
                    title=track["title"],
                    artist=track["artist"],
                    album=track["album"],
                    status="missing",
                    error=str(e),
                )
            )

    # Step 3: Add unique missing artists to Lidarr
    lidarr_queued_count = 0
    added_artist_names: set[str] = set()

    if lidarr and missing_artists:
        try:
            root_folder = await lidarr.get_root_folder()
            existing_artists = await lidarr.get_artists()
            existing_ids = {
                a.get("foreignArtistId")
                for a in existing_artists
                if a.get("foreignArtistId")
            }

            for artist_name, artist_info in missing_artists.items():
                try:
                    lookup_results = await lidarr.lookup_artist(artist_name)

                    if lookup_results:
                        artist_data = lookup_results[0]

                        if artist_data.get("foreignArtistId") in existing_ids:
                            logger.info(f"Artist already in Lidarr: {artist_name}")
                            continue

                        await lidarr.add_artist(artist_data, root_folder)
                        lidarr_queued_count += 1
                        added_artist_names.add(artist_name)
                        logger.info(f"Added artist to Lidarr: {artist_name}")

                except Exception as e:
                    logger.warning(f"Failed to add artist to Lidarr: {artist_name}: {e}")

        except Exception as e:
            logger.error(f"Lidarr integration failed: {e}")

    # Flag tracks whose artist was added to Lidarr
    for tr in track_results:
        if tr.status == "missing" and tr.artist in added_artist_names:
            tr.lidarr_artist_added = True

    # Step 4: Create or update Navidrome playlist
    navidrome_playlist_id = None
    if matched_track_ids:
        try:
            # Check if a playlist with this name already exists
            existing_playlists = await client.get_playlists()
            existing = None
            for pl in existing_playlists:
                if pl.get("name") == playlist_name:
                    existing = pl
                    break

            if existing:
                # Delete and recreate — Subsonic updatePlaylist.view
                # does not reliably replace tracks
                pl_id = existing["id"]
                await client.delete_playlist(pl_id)
                logger.info(f"Deleted existing playlist: {playlist_name} (id={pl_id})")

            # Create (or recreate) the playlist
            result = await client.create_playlist(playlist_name, matched_track_ids)
            navidrome_playlist_id = result.get("playlist", {}).get("id")
            logger.info(f"Created playlist: {playlist_name} (id={navidrome_playlist_id})")

        except Exception as e:
            logger.error(f"Failed to create/update Navidrome playlist: {e}")

    matched_count = sum(1 for r in track_results if r.status == "matched")
    missing_count = sum(1 for r in track_results if r.status == "missing")

    return YouTubeImportResult(
        playlist_name=playlist_name,
        navidrome_playlist_id=navidrome_playlist_id,
        matched_count=matched_count,
        missing_count=missing_count,
        lidarr_queued_count=lidarr_queued_count,
        tracks=track_results,
    )


# --- Lidarr Configuration Endpoints ---


@router.get("/lidarr-config", response_model=LidarrConfigResponse)
async def get_lidarr_config(db: Session = Depends(get_db)):
    """Get Lidarr connection configuration."""
    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if not config:
        return LidarrConfigResponse(configured=False)

    try:
        service = LidarrService(config.url, config.api_key)
        status = await service.test_connection()
        return LidarrConfigResponse(
            configured=True,
            url=config.url,
            connected=True,
            version=status.get("version"),
        )
    except Exception:
        return LidarrConfigResponse(configured=True, url=config.url, connected=False)


@router.put("/lidarr-config", response_model=LidarrConfigResponse)
async def set_lidarr_config(
    request: LidarrConfigRequest,
    db: Session = Depends(get_db),
):
    """Save Lidarr connection configuration."""
    import app.main as _main

    try:
        service = LidarrService(request.url, request.api_key)
        status = await service.test_connection()
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to connect to Lidarr: {e}"
        )

    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config:
        config.url = request.url
        config.api_key = request.api_key
    else:
        config = LidarrConfig(id=1, url=request.url, api_key=request.api_key)
        db.add(config)
    db.commit()

    _main.set_lidarr_service(service)

    return LidarrConfigResponse(
        configured=True,
        url=request.url,
        connected=True,
        version=status.get("version"),
    )


@router.delete("/lidarr-config", response_model=LidarrConfigResponse)
async def clear_lidarr_config(db: Session = Depends(get_db)):
    """Clear Lidarr connection configuration."""
    import app.main as _main

    db.query(LidarrConfig).filter(LidarrConfig.id == 1).delete()
    db.commit()
    _main.set_lidarr_service(None)
    return LidarrConfigResponse(configured=False)
