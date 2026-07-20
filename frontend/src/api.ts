// API client — calls FastAPI backend at /api/*

import type {
  NavidromeConfig,
  ConnectionStatus,
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
};