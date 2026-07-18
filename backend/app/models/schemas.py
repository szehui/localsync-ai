from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# --- Navidrome Connection ---

class NavidromeConfig(BaseModel):
    url: str
    username: str
    password: str


class ConnectionStatus(BaseModel):
    connected: bool
    message: str
    server_version: Optional[str] = None


# --- Library ---

class TrackResponse(BaseModel):
    id: str
    title: str
    album_id: Optional[str] = None
    album_name: Optional[str] = None
    artist_id: Optional[str] = None
    artist_name: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[int] = None
    duration: Optional[int] = None
    play_count: int = 0
    rating: int = 0
    starred: bool = False
    created_at: Optional[datetime] = None


class AlbumResponse(BaseModel):
    id: str
    name: str
    artist_name: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    song_count: int = 0
    play_count: int = 0
    created_at: Optional[datetime] = None


class ArtistResponse(BaseModel):
    id: str
    name: str
    album_count: int = 0


# --- Playlist Generation ---

class PlaylistGenerateRequest(BaseModel):
    seed_track_id: str
    track_count: int = 20
    strictness: int = 3  # 1-5


class PlaylistGenerateResponse(BaseModel):
    name: str
    tracks: list[TrackResponse]
    track_count: int


class PlaylistPushRequest(BaseModel):
    name: str
    track_ids: list[str]


class PlaylistPushResponse(BaseModel):
    playlist_id: str
    name: str
    track_count: int


# --- Smart Triggers ---

class TriggerCreate(BaseModel):
    name: str
    trigger_type: str  # "recency" | "heavy_rotation" | "scheduled"
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    playlist_name: Optional[str] = None


class TriggerUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    playlist_name: Optional[str] = None


class TriggerResponse(BaseModel):
    id: int
    name: str
    trigger_type: str
    enabled: bool
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    playlist_name: Optional[str] = None
    navidrome_playlist_id: Optional[str] = None
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    created_at: datetime


# --- Sync ---

class SyncStatus(BaseModel):
    is_syncing: bool = False
    last_sync: Optional[datetime] = None
    next_sync: Optional[datetime] = None
    track_count: int = 0
    album_count: int = 0
    artist_count: int = 0
    message: str = ""
