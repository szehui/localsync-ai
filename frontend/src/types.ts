// API type definitions matching backend schemas

export interface NavidromeConfig {
  url: string;
  username: string;
  password: string;
}

export interface ConnectionStatus {
  connected: boolean;
  message: string;
  server_version?: string;
}

export interface Track {
  id: string;
  title: string;
  album_id?: string;
  album_name?: string;
  artist_id?: string;
  artist_name?: string;
  genre?: string;
  year?: number;
  duration?: number;
  play_count: number;
  rating: number;
  starred: boolean;
  created_at?: string;
}

export interface Album {
  id: string;
  name: string;
  artist_name?: string;
  year?: number;
  genre?: string;
  song_count: number;
  play_count: number;
  created_at?: string;
}

export interface Artist {
  id: string;
  name: string;
  album_count: number;
}

export interface PlaylistGenerateRequest {
  seed_track_id: string;
  track_count?: number;
  strictness?: number;
}

export interface PlaylistGenerateResponse {
  name: string;
  tracks: Track[];
  track_count: number;
}

export interface PlaylistPushRequest {
  name: string;
  track_ids: string[];
}

export interface PlaylistPushResponse {
  playlist_id: string;
  name: string;
  track_count: number;
}

export interface PlaylistDetailResponse {
  id: number;
  name: string;
  navidrome_playlist_id?: string | null;
  seed_track_id?: string | null;
  seed_track_name?: string | null;
  seed_playlist_id?: string | null;
  seed_playlist_name?: string | null;
  strictness: number;
  track_count: number;
  created_at: string;
  updated_at: string;
}

export interface PlaylistFromPlaylistRequest {
  navidrome_playlist_id: string;
  track_count?: number;
  strictness?: number;
  per_seed_track?: number;
}

export interface NavidromePlaylist {
  id: string;
  name: string;
  song_count: number;
  owner: string;
  public: boolean;
  created?: string;
  cover_art?: string;
}

export interface GeneratedPlaylist {
  id: number;
  name: string;
  navidrome_playlist_id?: string;
  seed_track_name?: string;
  seed_playlist_id?: string;
  seed_playlist_name?: string;
  strictness: number;
  track_count: number;
  created_at: string;
  updated_at: string;
}

export interface TriggerCreate {
  name: string;
  trigger_type: 'recency' | 'heavy_rotation' | 'scheduled' | 'library_mix';
  cron_expression?: string;
  threshold?: number;
  /** library_mix only: which local mix to build */
  mix_kind?: MixKind;
  playlist_name?: string;
}

/** Local-only mixes built from the library and play counts — no upstream API. */
export type MixKind = 'deep_cuts' | 'on_repeat' | 'rewind' | 'rediscover' | 'unplayed';

export interface TriggerUpdate {
  name?: string;
  enabled?: boolean;
  cron_expression?: string;
  threshold?: number;
  mix_kind?: MixKind;
  playlist_name?: string;
}

export interface Trigger {
  id: number;
  name: string;
  trigger_type: string;
  enabled: boolean;
  cron_expression?: string;
  threshold?: number;
  mix_kind?: MixKind | null;
  playlist_name?: string;
  navidrome_playlist_id?: string;
  last_run?: string;
  next_run?: string;
  created_at: string;
}

export interface SyncStatus {
  is_syncing: boolean;
  last_sync?: string;
  next_sync?: string;
  track_count: number;
  album_count: number;
  artist_count: number;
  message?: string;
}

export interface LibraryStats {
  track_count: number;
  album_count: number;
  artist_count: number;
}

// ─── Auth ────────────────────────────────────────────────────────────────────

export interface LoginRequest {
  url: string;
  username: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface UserResponse {
  username: string;
  navidrome_url: string;
  server_version?: string;
  /** Read from the database on every /me call, never trusted from the JWT. */
  is_owner: boolean;
  auto_approve: boolean;
  can_request: boolean;
}

// ─── Discovery feed ──────────────────────────────────────────────────────────

export type DiscoveryBucket = 'new_artist' | 'artist_known';

export interface DiscoveryOfferSummary {
  id: number;
  recording_mbid: string;
  track_title?: string;
  release_name?: string;
  release_mbid?: string;
  score: number;
  source: string;
  status: string;
  lidarr_album_id?: number;
}

export interface DiscoveryEvidence {
  seed_artist: string;
  seed_title: string;
}

export interface DiscoveryArtistGroup {
  artist_name?: string;
  bucket: DiscoveryBucket;
  score: number;
  evidence_count: number;
  offer_count: number;
  representative_offer_id: number;
  representative_title?: string;
  representative_release?: string;
  evidence: DiscoveryEvidence[];
  offers: DiscoveryOfferSummary[];
}

export interface DiscoveryState {
  last_run?: string;
  last_status?: string;
  last_error?: string;
  seeds_used: number;
  candidates_seen: number;
  offers_added: number;
  calls_made: number;
  lb_seeds_used: number;
  offers_expired: number;
}

export interface DiscoveryFeed {
  artists: DiscoveryArtistGroup[];
  state: DiscoveryState;
  counts: {
    new_artist: number;
    artist_known: number;
    total_offers: number;
  };
}

export interface DiscoveryAddResult {
  offer_id: number;
  artist_name?: string;
  release_name?: string;
  album_id?: number;
  already_present: boolean;
  albums_unmonitored: number[];
  message: string;
}

// ─── Catalog (MusicBrainz-backed) ────────────────────────────────────────────

/** How a release group is known to be in the library, or null if it is not. */
export type OwnedVia = 'mbid' | 'name' | null;

export interface OpenRequestRef {
  id: number;
  status: RequestStatus;
  requested_by?: string | null;
}

export interface CatalogCard {
  release_group_mbid: string;
  title?: string | null;
  artist_name?: string | null;
  artist_mbid?: string | null;
  year?: number | null;
  primary_type?: string | null;
  secondary_types: string[];
  cover_url?: string | null;
  score?: number | null;
  owned: boolean;
  owned_via?: OwnedVia;
  request?: OpenRequestRef | null;
  /** The household has blocklisted this album, or its artist. */
  blocked?: boolean;
}

export interface ArtistSuggestion {
  artist_mbid: string;
  artist_name?: string | null;
  disambiguation?: string | null;
}

export interface CatalogSearchResponse {
  results: CatalogCard[];
  count: number;
  offset: number;
  artist_suggestions: ArtistSuggestion[];
}

export interface ReleaseEdition {
  id: string;
  title?: string | null;
  date?: string | null;
  country?: string | null;
}

export interface AlbumDetail extends CatalogCard {
  aliases: string[];
  /** MusicBrainz says the type is album/EP — singles and compilations are not. */
  requestable: boolean;
  releases: ReleaseEdition[];
}

export interface ArtistDetail {
  artist_mbid: string;
  artist_name?: string | null;
  disambiguation?: string | null;
  albums: CatalogCard[];
  summary: {
    albums: number;
    owned: number;
    requestable: number;
  };
}

export interface RailsResponse {
  new_releases: CatalogCard[];
  trending: CatalogCard[];
}

// ─── Requests ────────────────────────────────────────────────────────────────

/**
 * The server's state machine, verbatim. The UI renders these strings and never
 * infers a status from timestamps or from what the user just clicked.
 */
export type RequestStatus =
  | 'pending'
  | 'approved'
  | 'downloading'
  | 'partially_available'
  | 'imported'
  | 'available'
  | 'failed'
  | 'denied'
  | 'cancelled';

export type RequestKind = 'album' | 'artist';
export type ArtistScope = 'studio' | 'studio_eps' | 'everything';

export interface RequestItem {
  id: number;
  kind: RequestKind;
  status: RequestStatus;
  release_group_mbid?: string | null;
  artist_mbid?: string | null;
  artist_name?: string | null;
  album_title?: string | null;
  year?: number | null;
  primary_type?: string | null;
  cover_url?: string | null;
  scope?: ArtistScope | null;
  note?: string | null;
  requested_by?: string | null;
  requested_at?: string | null;
  decided_at?: string | null;
  decided_by?: string | null;
  vote_count: number;
  last_error?: string | null;
  lidarr_album_id?: number | null;
  rollup: Record<string, number>;
  children: RequestItem[];
}

export interface RequestListResponse {
  requests: RequestItem[];
  total: number;
}

export interface RequestCreatePayload {
  kind: RequestKind;
  release_group_mbid?: string;
  artist_mbid?: string;
  scope?: ArtistScope;
  note?: string;
}

export interface RequestVoteResponse {
  request_id: number;
  voted: boolean;
  added: boolean;
  vote_count: number;
}

export interface ArtistPlanAlbum {
  release_group_mbid: string;
  title?: string | null;
  year?: number | null;
  primary_type?: string | null;
}

export interface ArtistRequestPlan {
  artist_mbid: string;
  artist_name?: string | null;
  disambiguation?: string | null;
  scope: ArtistScope;
  albums: ArtistPlanAlbum[];
  summary: Record<string, number>;
}

export interface ReconcileResult {
  total: number;
  changed: number;
  failed: number;
}

// ─── Notifications ──────────────────────────────────────────────────────────

export type NotificationEvent =
  | 'request.created'
  | 'request.available'
  | 'request.failed';

export interface NotificationConfig {
  configured: boolean;
  enabled: boolean;
  /** A masked form of the webhook URL: the token is never sent to the client. */
  webhook_hint?: string | null;
  events: NotificationEvent[];
  available_events: NotificationEvent[];
  base_url?: string | null;
}

export interface NotificationConfigPayload {
  /** Omitted means "keep the stored URL" — the API never returns it. */
  webhook_url?: string;
  enabled: boolean;
  events?: NotificationEvent[];
  base_url?: string;
}

export interface NotificationTestResult {
  sent: boolean;
  detail: string;
}

// ─── Blocklist ──────────────────────────────────────────────────────────────

export type BlocklistScope = 'release_group' | 'artist';

export interface BlocklistEntry {
  id: number;
  scope: BlocklistScope;
  release_group_mbid?: string | null;
  artist_mbid?: string | null;
  artist_name?: string | null;
  album_title?: string | null;
  reason?: string | null;
  created_by?: string | null;
  created_at?: string | null;
}

export interface BlocklistListResponse {
  entries: BlocklistEntry[];
  total: number;
}

export interface BlocklistCreatePayload {
  scope: BlocklistScope;
  release_group_mbid?: string;
  artist_mbid?: string;
  artist_name?: string;
  album_title?: string;
  reason?: string;
}
