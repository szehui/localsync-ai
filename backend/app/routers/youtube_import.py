"""YouTube Music import router."""
import asyncio
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
from app.services.youtube_music import (
    youtube_music_service,
    clean_artist_name,
    clean_track_title,
)
from app.services.lidarr import LidarrService
from app.services.deezer import DeezerService
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


# A correct title with the wrong artist is a wrong song: "Celebration" by Kool &
# The Gang must not resolve to They Might Be Giants just because the title is
# exact. Require some artist agreement whenever we know the artist at all.
_ARTIST_GATE = 0.45


def _artist_similarity(artist1: str, artist2: str) -> float:
    """Similarity of two artist names, tolerant of word splits and stop words."""
    norm1 = _normalize_text(artist1)
    norm2 = _normalize_text(artist2)
    seq = SequenceMatcher(None, norm1, norm2).ratio()
    tok1 = _tokenize(artist1)
    tok2 = _tokenize(artist2)
    if not tok1 or not tok2:
        tok = 1.0 if norm1 == norm2 else 0.0
    else:
        tok = len(tok1 & tok2) / len(tok1 | tok2)
    return max(seq, tok)


def _artist_agrees(yt_artist: str, lib_artist: str) -> bool:
    """True when the library artist plausibly matches the YouTube artist.

    Unknown YouTube artists pass: with no artist to compare there is nothing to
    gate on, and a title match is the only evidence available.
    """
    if not (yt_artist or "").strip():
        return True
    return _artist_similarity(yt_artist, lib_artist) >= _ARTIST_GATE


def _preference_key(raw_title: str, clean_title: str, raw_score: float) -> tuple:
    """Rank two equally-scoring candidates.

    Cleaning makes "Song", "Song (Live; 2010 Remaster)" and "Song (2001 Remaster)"
    all score 1.0, so the importer can otherwise drop a live take into a playlist
    when a studio copy exists. Preferring the candidate that needed no cleaning is
    the reliable signal: a raw string comparison actually favours the live copy,
    because "2001 Remaster" resembles "Live; 2010 Remaster" more than it
    resembles the bare title.
    """
    needed_no_cleaning = 1.0 if (raw_title or "").strip() == (clean_title or "").strip() else 0.0
    return (needed_no_cleaning, raw_score)


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


def _group_by_album(
    tracks: List["TrackMatchResult"],
    hits: List[Optional[Dict[str, Any]]],
) -> tuple[Dict[tuple, Dict[str, Any]], List["TrackMatchResult"]]:
    """Group tracks by the release they belong to.

    Returns ``(albums, unresolved)`` where ``albums`` maps a case-folded
    ``(album_artist, album)`` key to ``{"album_artist", "album", "tracks"}``.
    Tracks with no attribution land in ``unresolved``.

    Grouping matters: a 300-track playlist wants ~90 albums, not 130, and each
    duplicate add costs a Lidarr lookup plus a monitoring collapse.
    """
    albums: Dict[tuple, Dict[str, Any]] = {}
    unresolved: List[TrackMatchResult] = []

    for track, hit in zip(tracks, hits):
        if not hit:
            unresolved.append(track)
            continue
        track.album = hit["album"]
        key = (hit["album_artist"].casefold(), hit["album"].casefold())
        group = albums.setdefault(key, {
            "album_artist": hit["album_artist"],
            "album": hit["album"],
            "tracks": [],
        })
        group["tracks"].append(track)

    return albums, unresolved


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

    for track in playlist_data["tracks"]:
        try:
            # Search strategy: title-only first (most flexible), then
            # combined, then artist-only. Verify artist match via fuzzy scoring.
            #
            # Queries use the CLEANED title: Navidrome's search index rarely
            # matches "(2013 Remaster)" / "[2023 Remaster]" suffixes, so searching
            # the raw YouTube title returns nothing for songs we own.
            songs: List[dict] = []
            search_title = clean_track_title(track["title"])
            search_artist = clean_artist_name(track["artist"])

            # Strategy 1: title-only (best for artist name variations)
            if search_title:
                title_result = await client.search(
                    query=search_title,
                    artist_count=0,
                    album_count=0,
                    song_count=20,
                )
                songs = title_result.get("searchResult3", {}).get("song", [])

            # Strategy 2: combined artist+title
            if not songs and search_artist and search_title:
                combo_result = await client.search(
                    query=f"{search_artist} {search_title}",
                    artist_count=0,
                    album_count=0,
                    song_count=10,
                )
                songs = combo_result.get("searchResult3", {}).get("song", [])

            # Strategy 3: artist-only (broad, last resort)
            if not songs and search_artist:
                artist_result = await client.search(
                    query=search_artist,
                    artist_count=5,
                    album_count=0,
                    song_count=20,
                )
                songs = artist_result.get("searchResult3", {}).get("song", [])

            # Find best match across all candidates
            best_match = None
            best_score = 0.0
            best_pref = (-1.0, -1.0)

            for song in songs:
                song_title = clean_track_title(song.get("title", ""))
                song_artist = clean_artist_name(song.get("artist", ""))
                score = _match_score(
                    search_title,
                    search_artist,
                    song_title,
                    song_artist,
                )
                if score < 0.75:
                    continue
                # A perfect title with an unrelated artist is a different song.
                if not _artist_agrees(search_artist, song_artist):
                    continue
                # Among equally-scoring copies (studio vs live vs remaster),
                # prefer the one that needed no cleaning.
                raw_title = song.get("title", "")
                pref = _preference_key(
                    raw_title,
                    song_title,
                    _match_score(track["title"], track["artist"],
                                 raw_title, song.get("artist", "")),
                )
                if score > best_score or (
                    score == best_score and pref > best_pref
                ):
                    best_score = score
                    best_pref = pref
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

    # Step 3: Attribute missing tracks to albums, then add those albums
    #
    # Album-level, never artist-level: `add_artist` subscribes to a whole
    # discography (one missing BTS track would queue all 30 albums). The flat
    # YouTube feed carries no album metadata, so each missing track is resolved
    # to a release via Deezer first, then the distinct releases go to Lidarr in
    # one batch — `add_albums` collapses monitoring once per artist instead of
    # once per album, which in a loop would un-monitor its own earlier adds.
    lidarr_queued_count = 0
    lidarr_albums: List[str] = []
    unresolved_tracks: List[TrackMatchResult] = []

    missing_results = [r for r in track_results if r.status == "missing"]
    if lidarr and missing_results:
        try:
            deezer = DeezerService()
            sem = asyncio.Semaphore(5)

            async def _attribute(tr: TrackMatchResult):
                async with sem:
                    try:
                        return await deezer.lookup_album(
                            clean_artist_name(tr.artist),
                            clean_track_title(tr.title),
                        )
                    except Exception as e:
                        logger.warning(
                            "Album attribution failed for %s - %s: %s",
                            tr.artist, tr.title, e)
                        return None

            hits = await asyncio.gather(
                *[_attribute(tr) for tr in missing_results])

            albums, unresolved = _group_by_album(missing_results, hits)

            if albums:
                outcome = await lidarr.add_albums(
                    [(g["album_artist"], g["album"]) for g in albums.values()],
                    search=True,
                )
                added_keys = {
                    (r.get("artist_name", "").casefold(),
                     (r.get("release_name") or "").casefold())
                    for r in outcome["results"] if r.get("album_id")
                }
                for group in albums.values():
                    name = f'{group["album_artist"]} — {group["album"]}'
                    if (group["album_artist"].casefold(),
                            group["album"].casefold()) in added_keys:
                        lidarr_albums.append(name)
                        for tr in group["tracks"]:
                            tr.lidarr_album_added = True
                            tr.status = "queued_to_lidarr"
                    else:
                        unresolved_tracks.extend(group["tracks"])

                lidarr_queued_count = len(lidarr_albums)
                logger.info(
                    "Lidarr: %d album(s) added for %d missing track(s), "
                    "%d failed, monitoring_ok=%s",
                    len(outcome["added"]) + len(outcome["already_present"]),
                    len(missing_results), len(outcome["failed"]),
                    outcome["monitoring_ok"],
                )

        except Exception as e:
            logger.error(f"Lidarr integration failed: {e}")

    # Tracks we could neither find on an album nor resolve stay missing
    if lidarr and unresolved_tracks:
        logger.info("Lidarr: %d track(s) left unresolved (no album found)",
                    len(unresolved_tracks))

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
    # "Missing" stays truthful: queued-to-Lidarr tracks are still not in the
    # library, they just have a download on the way.
    missing_count = sum(
        1 for r in track_results if r.status in ("missing", "queued_to_lidarr"))

    return YouTubeImportResult(
        playlist_name=playlist_name,
        navidrome_playlist_id=navidrome_playlist_id,
        matched_count=matched_count,
        missing_count=missing_count,
        lidarr_queued_count=lidarr_queued_count,
        lidarr_albums=lidarr_albums,
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
