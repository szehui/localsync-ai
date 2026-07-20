from pydantic import BaseModel, ConfigDict
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
    model_config = ConfigDict(from_attributes=True)

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
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    artist_name: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    song_count: int = 0
    play_count: int = 0
    created_at: Optional[datetime] = None


class ArtistResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class NavidromePlaylist(BaseModel):
    id: str
    name: str
    song_count: int
    owner: str
    public: bool
    created: Optional[str] = None
    cover_art: Optional[str] = None


class PlaylistFromPlaylistRequest(BaseModel):
    navidrome_playlist_id: str
    track_count: int = 30
    strictness: int = 3
    per_seed_track: int = 5  # similar songs to pull per seed track


class PlaylistDetailResponse(BaseModel):
    id: int
    name: str
    navidrome_playlist_id: Optional[str] = None
    seed_track_id: Optional[str] = None
    seed_track_name: Optional[str] = None
    seed_playlist_id: Optional[str] = None
    seed_playlist_name: Optional[str] = None
    strictness: int
    track_count: int
    created_at: datetime
    updated_at: datetime


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
    model_config = ConfigDict(from_attributes=True)

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


# --- Web Auth (JWT) ---

class LoginRequest(BaseModel):
    url: str
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    username: str
    navidrome_url: str
    server_version: Optional[str] = None


# --- Sync ---

class SyncStatus(BaseModel):
    is_syncing: bool = False
    last_sync: Optional[datetime] = None
    next_sync: Optional[datetime] = None
    track_count: int = 0
    album_count: int = 0
    artist_count: int = 0
    message: str = ""
