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
    trigger_type: str  # "recency" | "heavy_rotation" | "scheduled" | "library_mix"
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    # library_mix only: deep_cuts | on_repeat | rewind | rediscover | unplayed
    mix_kind: Optional[str] = None
    playlist_name: Optional[str] = None


class TriggerUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    mix_kind: Optional[str] = None
    playlist_name: Optional[str] = None


class TriggerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    trigger_type: str
    enabled: bool
    cron_expression: Optional[str] = None
    threshold: Optional[int] = None
    mix_kind: Optional[str] = None
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
    # The caller's own permissions. The UI needs these to decide what to offer —
    # an Approve button that 403s on click is worse than no button at all. They
    # are read from the database on every call rather than taken from the JWT,
    # because `is_owner` is a mutable flag and a stale token must not carry
    # elevated rights.
    is_owner: bool = False
    auto_approve: bool = False
    can_request: bool = True


# --- Household roster ---

class HouseholdUserResponse(BaseModel):
    """One account in the household, as shown on the roster.

    Deliberately narrow: no ``navidrome_password`` and no ListenBrainz token
    field exists here at all, so a future mistake cannot start leaking them —
    there is no attribute to populate. ``listenbrainz_configured`` is a boolean
    derived from row existence, not the credential itself.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: Optional[str] = None
    is_owner: bool = False
    # Request policy, visible so a member can see why their button is disabled.
    auto_approve: bool = False
    can_request: bool = True
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None
    listenbrainz_configured: bool = False


class DeleteUserResponse(BaseModel):
    deleted: bool
    user_id: int


class UserFlagsUpdate(BaseModel):
    """Owner-only policy change for another household member.

    Both fields are optional so a PATCH can flip one without restating the
    other, and an empty body is rejected rather than silently doing nothing.
    """

    auto_approve: Optional[bool] = None
    can_request: Optional[bool] = None


# --- Last.fm ---


class LastfmConfigRequest(BaseModel):
    api_key: str
    username: str


class LastfmConfigResponse(BaseModel):
    configured: bool
    username: Optional[str] = None


# --- ListenBrainz ---


class ListenbrainzConfigRequest(BaseModel):
    username: str
    token: str


class ListenbrainzConfigResponse(BaseModel):
    configured: bool
    username: Optional[str] = None


class ListenbrainzTestResponse(BaseModel):
    valid: bool
    username: Optional[str] = None
    listen_count: int = 0
    message: str = ""


# --- Sync ---

class SyncStatus(BaseModel):
    is_syncing: bool = False
    last_sync: Optional[datetime] = None
    next_sync: Optional[datetime] = None
    track_count: int = 0
    album_count: int = 0
    artist_count: int = 0
    message: str = ""


# --- Discovery feed ---

class DiscoveryOfferSummary(BaseModel):
    id: int
    recording_mbid: str
    track_title: Optional[str] = None
    release_name: Optional[str] = None
    release_mbid: Optional[str] = None
    score: float = 0.0
    source: str = "labs"
    status: str = "offered"
    lidarr_album_id: Optional[int] = None


class DiscoveryArtistGroup(BaseModel):
    artist_name: Optional[str] = None
    bucket: str  # "new_artist" | "artist_known"
    score: float = 0.0
    evidence_count: int = 0
    offer_count: int = 0
    representative_offer_id: int
    representative_title: Optional[str] = None
    representative_release: Optional[str] = None
    evidence: list[dict] = []
    offers: list[DiscoveryOfferSummary] = []


class DiscoveryStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_run: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    seeds_used: int = 0
    candidates_seen: int = 0
    offers_added: int = 0
    calls_made: int = 0
    lb_seeds_used: int = 0
    offers_expired: int = 0


class DiscoveryCounts(BaseModel):
    new_artist: int = 0
    artist_known: int = 0
    total_offers: int = 0


class DiscoveryFeedResponse(BaseModel):
    artists: list[DiscoveryArtistGroup] = []
    state: DiscoveryStateResponse
    counts: DiscoveryCounts


class DiscoveryRefreshResponse(BaseModel):
    status: str
    message: str = ""


class DiscoveryAddRequest(BaseModel):
    search: bool = False


class DiscoveryAddResponse(BaseModel):
    offer_id: int
    artist_name: Optional[str] = None
    release_name: Optional[str] = None
    album_id: Optional[int] = None
    already_present: bool = False
    albums_unmonitored: list[int] = []
    message: str = ""


# --- Requests ---

class RequestCreate(BaseModel):
    """Ask for an album (release_group_mbid) or a discography (artist_mbid)."""

    kind: str = "album"                     # album | artist
    release_group_mbid: Optional[str] = None
    artist_mbid: Optional[str] = None
    scope: Optional[str] = None             # artist: studio | studio_eps | everything
    note: Optional[str] = None


class RequestResponse(BaseModel):
    """One request, shaped so a list needs no catalog or Lidarr call."""

    id: int
    kind: str
    status: str
    release_group_mbid: Optional[str] = None
    artist_mbid: Optional[str] = None
    artist_name: Optional[str] = None
    album_title: Optional[str] = None
    year: Optional[int] = None
    primary_type: Optional[str] = None
    cover_url: Optional[str] = None
    scope: Optional[str] = None
    note: Optional[str] = None
    requested_by: Optional[str] = None
    requested_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    vote_count: int = 0
    last_error: Optional[str] = None
    lidarr_album_id: Optional[int] = None
    # Artist requests only: per-status child counts, computed on read so a child
    # retried outside the parent's flow cannot leave it stale.
    rollup: dict[str, int] = {}
    children: list["RequestResponse"] = []


class RequestListResponse(BaseModel):
    requests: list[RequestResponse]
    total: int


class RequestVoteResponse(BaseModel):
    request_id: int
    voted: bool
    added: bool = False
    vote_count: int = 0


class ArtistRequestPlan(BaseModel):
    """What a discography request would create, before it is created."""

    artist_mbid: str
    artist_name: Optional[str] = None
    disambiguation: Optional[str] = None
    scope: str
    albums: list[dict] = []
    summary: dict[str, int] = {}


# --- Notifications ---

class NotificationConfigRequest(BaseModel):
    """Configure the household webhook.

    ``webhook_url`` may be omitted on update to keep the stored one: the API
    never returns it, so a UI that round-trips the response would otherwise
    write back a masked placeholder over a working URL.
    """

    webhook_url: Optional[str] = None
    enabled: bool = True
    events: Optional[list[str]] = None
    base_url: Optional[str] = None


class NotificationConfigResponse(BaseModel):
    """Never carries the webhook URL — only a masked hint and its state."""

    configured: bool = False
    enabled: bool = False
    webhook_hint: Optional[str] = None
    events: list[str] = []
    available_events: list[str] = []
    base_url: Optional[str] = None


class NotificationTestResponse(BaseModel):
    sent: bool
    detail: str


# --- Blocklist ---

class BlocklistCreate(BaseModel):
    """Block one release group or one artist. Exactly one id is required."""

    scope: str = "release_group"          # release_group | artist
    release_group_mbid: Optional[str] = None
    artist_mbid: Optional[str] = None
    reason: Optional[str] = None
    # Display fields, so the owner can recognise the row later without a
    # MusicBrainz call per list render. They are hints, not identity.
    artist_name: Optional[str] = None
    album_title: Optional[str] = None


class BlocklistEntry(BaseModel):
    id: int
    scope: str
    release_group_mbid: Optional[str] = None
    artist_mbid: Optional[str] = None
    artist_name: Optional[str] = None
    album_title: Optional[str] = None
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None


class BlocklistListResponse(BaseModel):
    entries: list[BlocklistEntry] = []
    total: int = 0
