// API client — calls FastAPI backend at /api/*

import type {
  Track,
  Album,
  Artist,
  PlaylistGenerateRequest,
  PlaylistGenerateResponse,
  PlaylistPushRequest,
  PlaylistPushResponse,
  GeneratedPlaylist,
  TriggerCreate,
  TriggerUpdate,
  Trigger,
  LibraryStats,
  SyncStatus,
  NavidromePlaylist,
  PlaylistFromPlaylistRequest,
  LoginRequest,
  TokenResponse,
  UserResponse,
  ConnectionStatus,
  DiscoveryFeed,
  DiscoveryAddResult,
  AlbumDetail,
  ArtistDetail,
  ArtistRequestPlan,
  CatalogSearchResponse,
  RailsResponse,
  ReconcileResult,
  RequestCreatePayload,
  RequestItem,
  RequestListResponse,
  RequestVoteResponse,
  ArtistScope,
} from './types';

const BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem('access_token');
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };

  const res = await fetch(`${BASE}${path}`, {
    headers,
    ...options,
  });
  if (!res.ok) {
    // The backend's refusals carry `{detail: {code, message}}` for requests and
    // a plain string elsewhere. Surface the message, never the raw object — a
    // banner reading "[object Object]" is worse than the status code.
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = err?.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || detail?.code || res.statusText;
    throw new Error(message);
  }
  return res.json();
}

// ─── Auth ────────────────────────────────────────────────────────────────────

export const api = {
  // Auth
  login: (creds: LoginRequest) =>
    request<TokenResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(creds),
    }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request<UserResponse>('/auth/me'),
  connectionStatus: () =>
    request<ConnectionStatus>('/auth/status'),

  // Library
  getTracks: (params?: {
    search?: string;
    artist_id?: string;
    album_id?: string;
    genre?: string;
    sort?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.artist_id) qs.set('artist_id', params.artist_id);
    if (params?.album_id) qs.set('album_id', params.album_id);
    if (params?.genre) qs.set('genre', params.genre);
    if (params?.sort) qs.set('sort', params.sort);
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Track[]>(`/library/tracks?${qs}`);
  },

  getTrack: (id: string) =>
    request<Track>(`/library/tracks/${id}`),

  getAlbums: (params?: {
    search?: string;
    sort?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.sort) qs.set('sort', params.sort);
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Album[]>(`/library/albums?${qs}`);
  },

  getArtists: (params?: {
    search?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Artist[]>(`/library/artists?${qs}`);
  },

  getLibraryStats: () =>
    request<LibraryStats>('/library/stats'),

  // Sync
  triggerSync: () =>
    request<SyncStatus>('/auth/sync', {
      method: 'POST',
    }),

  getSyncStatus: () =>
    request<SyncStatus>('/auth/sync-status'),

  // Playlists
  generatePlaylist: (req: PlaylistGenerateRequest) =>
    request<PlaylistGenerateResponse>('/playlists/generate', {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  pushPlaylist: (req: PlaylistPushRequest) =>
    request<PlaylistPushResponse>('/playlists/push', {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  getGeneratedPlaylists: () => request<GeneratedPlaylist[]>('/playlists/'),
  getPlaylistTracks: (playlistId: number) =>
    request<Track[]>(`/playlists/${playlistId}/tracks`),
  listNavidromePlaylists: () =>
    request<NavidromePlaylist[]>('/playlists/navidrome'),
  generateFromPlaylist: (req: PlaylistFromPlaylistRequest) =>
    request<PlaylistGenerateResponse>('/playlists/generate-from-playlist', {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // Triggers
  getTriggers: () => request<Trigger[]>('/triggers/'),
  createTrigger: (trigger: TriggerCreate) =>
    request<Trigger>('/triggers/', {
      method: 'POST',
      body: JSON.stringify(trigger),
    }),
  updateTrigger: (id: number, update: TriggerUpdate) =>
    request<Trigger>(`/triggers/${id}`, {
      method: 'PUT',
      body: JSON.stringify(update),
    }),
  deleteTrigger: (id: number) =>
    request<{ status: string }>(`/triggers/${id}`, {
      method: 'DELETE',
    }),
  toggleTrigger: (id: number) =>
    request<{ id: number; enabled: boolean }>(`/triggers/${id}/toggle`, {
      method: 'POST',
    }),

  // Last.fm
  getLastfmConfig: () =>
    request<{ configured: boolean; username?: string }>('/auth/lastfm'),
  setLastfmConfig: (req: { api_key: string; username: string }) =>
    request<{ configured: boolean; username?: string }>('/auth/lastfm', {
      method: 'PUT',
      body: JSON.stringify(req),
    }),
  clearLastfmConfig: () =>
    request<{ configured: boolean }>('/auth/lastfm', {
      method: 'DELETE',
    }),

  // ListenBrainz
  getListenbrainzConfig: () =>
    request<{ configured: boolean; username?: string }>('/auth/listenbrainz'),
  setListenbrainzConfig: (req: { username: string; token: string }) =>
    request<{ configured: boolean; username?: string }>('/auth/listenbrainz', {
      method: 'PUT',
      body: JSON.stringify(req),
    }),
  clearListenbrainzConfig: () =>
    request<{ configured: boolean }>('/auth/listenbrainz', {
      method: 'DELETE',
    }),
  testListenbrainzConfig: () =>
    request<{ valid: boolean; username?: string; listen_count: number; message: string }>(
      '/auth/listenbrainz/test',
      { method: 'POST' },
    ),

  // YouTube Import
  parseYouTubePlaylist: (req: { url: string }) =>
    request<{ playlist_title: string; track_count: number; tracks: Array<{
      title: string;
      artist: string;
      album: string;
      duration: number;
      youtube_id: string;
    }> }>('/youtube-import/parse', {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  importYouTubePlaylist: (req: { url: string; playlist_name?: string }) =>
    request<{
      playlist_name: string;
      navidrome_playlist_id?: string;
      matched_count: number;
      missing_count: number;
      lidarr_queued_count: number;
      lidarr_albums: string[];
      lidarr_artists: string[];
      tracks: Array<{
        title: string;
        artist: string;
        album: string;
        status: 'matched' | 'missing' | 'queued_to_lidarr';
        lidarr_album_added: boolean;
        lidarr_artist_added: boolean;
        error?: string;
      }>;
    }>('/youtube-import/import', {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // Lidarr Config
  getLidarrConfig: () =>
    request<{ configured: boolean; url?: string; connected?: boolean; version?: string }>(
      '/youtube-import/lidarr-config'
    ),

  setLidarrConfig: (req: { url: string; api_key: string }) =>
    request<{ configured: boolean; url?: string; connected?: boolean; version?: string }>(
      '/youtube-import/lidarr-config',
      {
        method: 'PUT',
        body: JSON.stringify(req),
      }
    ),

  clearLidarrConfig: () =>
    request<{ configured: boolean }>('/youtube-import/lidarr-config', {
      method: 'DELETE',
    }),

  // Discovery feed
  getDiscoveryFeed: (limit?: number) => {
    const qs = limit ? `?limit=${limit}` : '';
    return request<DiscoveryFeed>(`/discovery/${qs}`);
  },

  refreshDiscovery: () =>
    request<{ status: string; message: string }>('/discovery/refresh', {
      method: 'POST',
    }),

  dismissDiscoveryOffer: (offerId: number) =>
    request<{ status: string; message: string }>(
      `/discovery/${offerId}/dismiss`,
      { method: 'POST' },
    ),

  addDiscoveryOfferToLidarr: (offerId: number, search = false) =>
    request<DiscoveryAddResult>(`/discovery/${offerId}/add`, {
      method: 'POST',
      body: JSON.stringify({ search }),
    }),

  // ─── Catalog ──────────────────────────────────────────────────────────────
  //
  // Search takes a signal so a fast typist's abandoned queries are actually
  // cancelled: without it, a slow early keystroke can land after a later one and
  // replace correct results with stale ones.

  searchCatalog: (q: string, opts?: { limit?: number; offset?: number; signal?: AbortSignal }) => {
    const qs = new URLSearchParams({ q });
    if (opts?.limit) qs.set('limit', String(opts.limit));
    if (opts?.offset) qs.set('offset', String(opts.offset));
    return request<CatalogSearchResponse>(`/catalog/search?${qs}`, {
      signal: opts?.signal,
    });
  },

  getAlbum: (releaseGroupMbid: string) =>
    request<AlbumDetail>(`/catalog/album/${encodeURIComponent(releaseGroupMbid)}`),

  getArtist: (artistMbid: string) =>
    request<ArtistDetail>(`/catalog/artist/${encodeURIComponent(artistMbid)}`),

  getRails: () => request<RailsResponse>('/catalog/rails'),

  // ─── Requests ─────────────────────────────────────────────────────────────

  createRequest: (payload: RequestCreatePayload) =>
    request<RequestItem>('/requests', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  listRequests: (opts?: { scope?: 'mine' | 'all'; status?: string }) => {
    const qs = new URLSearchParams();
    if (opts?.scope) qs.set('scope', opts.scope);
    if (opts?.status) qs.set('status', opts.status);
    const suffix = qs.toString() ? `?${qs}` : '';
    return request<RequestListResponse>(`/requests${suffix}`);
  },

  getRequest: (id: number) => request<RequestItem>(`/requests/${id}`),

  voteRequest: (id: number) =>
    request<RequestVoteResponse>(`/requests/${id}/vote`, { method: 'POST' }),

  approveRequest: (id: number) =>
    request<RequestItem>(`/requests/${id}/approve`, { method: 'POST' }),

  denyRequest: (id: number, reason?: string) => {
    const qs = reason ? `?reason=${encodeURIComponent(reason)}` : '';
    return request<RequestItem>(`/requests/${id}/deny${qs}`, { method: 'POST' });
  },

  cancelRequest: (id: number) =>
    request<RequestItem>(`/requests/${id}/cancel`, { method: 'POST' }),

  retryRequest: (id: number) =>
    request<RequestItem>(`/requests/${id}/retry`, { method: 'POST' }),

  syncRequests: () => request<ReconcileResult>('/requests/sync', { method: 'POST' }),

  planArtistRequest: (artistMbid: string, scope: ArtistScope) => {
    const qs = new URLSearchParams({ artist_mbid: artistMbid, scope });
    return request<ArtistRequestPlan>(`/requests/plan?${qs}`);
  },
};
