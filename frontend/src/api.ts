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
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
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
};
