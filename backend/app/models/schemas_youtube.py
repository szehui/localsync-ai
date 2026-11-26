"""Pydantic schemas for YouTube import and Lidarr."""
from typing import List, Optional
from pydantic import BaseModel


# --- YouTube Music Import ---

class YouTubeTrack(BaseModel):
    title: str
    artist: str
    album: str
    duration: int  # seconds
    youtube_id: str


class YouTubeParseResponse(BaseModel):
    playlist_title: str
    track_count: int
    tracks: List[YouTubeTrack]


class YouTubeImportRequest(BaseModel):
    url: str
    playlist_name: Optional[str] = None  # Custom name for Navidrome playlist


class TrackMatchResult(BaseModel):
    title: str
    artist: str
    album: str
    status: str  # "matched" | "missing" | "queued_to_lidarr"
    lidarr_album_added: bool = False
    lidarr_artist_added: bool = False
    error: Optional[str] = None


class YouTubeImportResult(BaseModel):
    playlist_name: str
    navidrome_playlist_id: Optional[str] = None
    matched_count: int
    missing_count: int
    lidarr_queued_count: int  # number of albums added (not artists)
    lidarr_albums: List[str] = []
    lidarr_artists: List[str] = []  # fallback adds for unattributable tracks
    tracks: List[TrackMatchResult]


# --- Lidarr Configuration ---

class LidarrConfigRequest(BaseModel):
    url: str
    api_key: str


class LidarrConfigResponse(BaseModel):
    configured: bool
    url: Optional[str] = None
    connected: Optional[bool] = None
    version: Optional[str] = None


class LidarrArtistLookup(BaseModel):
    artist_name: str
    foreign_artist_id: str
    artist_metadata_id: int
    albums: List[dict] = []