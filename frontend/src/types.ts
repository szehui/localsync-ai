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

export interface GeneratedPlaylist {
  id: number;
  name: string;
  navidrome_playlist_id?: string;
  seed_track_name?: string;
  strictness: number;
  track_count: number;
  created_at: string;
  updated_at: string;
}

export interface TriggerCreate {
  name: string;
  trigger_type: 'recency' | 'heavy_rotation' | 'scheduled';
  cron_expression?: string;
  threshold?: number;
  playlist_name?: string;
}

export interface TriggerUpdate {
  name?: string;
  enabled?: boolean;
  cron_expression?: string;
  threshold?: number;
  playlist_name?: string;
}

export interface Trigger {
  id: number;
  name: string;
  trigger_type: string;
  enabled: boolean;
  cron_expression?: string;
  threshold?: number;
  playlist_name?: string;
  navidrome_playlist_id?: string;
  last_run?: string;
  next_run?: string;
  created_at: string;
}

export interface SyncStatus {
  last_sync?: string;
  next_sync?: string;
  track_count: number;
  album_count: number;
  artist_count: number;
  is_syncing: boolean;
}

export interface LibraryStats {
  track_count: number;
  album_count: number;
  artist_count: number;
}
