import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { Card, Button, Badge, Spinner, formatDate } from '../components';
import type { GeneratedPlaylist } from '../types';

export function SourceDashboard() {
  const [library, setLibrary] = useState<any>(null);
  const [status, setStatus] = useState<{ syncing: boolean; lastSync?: string; message?: string }>({
    syncing: false,
  });
  const [playlists, setPlaylists] = useState<GeneratedPlaylist[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadLibrary = useCallback(async () => {
    try {
      const data = await api.getLibraryStats();
      setLibrary(data);
    } catch (e: any) {
      console.warn('Library stats failed:', e);
    }
  }, []);

  const loadPlaylists = useCallback(async () => {
    try {
      setRefreshing(true);
      const data = await api.getGeneratedPlaylists();
      setPlaylists(data);
      setError(null);
    } catch (e: any) {
      setError(e.message || 'Failed to load playlists');
    } finally {
      setRefreshing(false);
    }
  }, []);

  const loadSyncStatus = useCallback(async () => {
    try {
      const data = await api.getSyncStatus();
      setStatus({ syncing: data.is_syncing, lastSync: data.last_sync, message: data.message });
    } catch (e: any) {
      console.warn('Sync status failed:', e);
    }
  }, []);

  useEffect(() => {
    loadLibrary();
    loadPlaylists();
    loadSyncStatus();
    const interval = setInterval(loadSyncStatus, 15000);
    return () => clearInterval(interval);
  }, [loadLibrary, loadPlaylists, loadSyncStatus]);

  const handleRefresh = async () => {
    await Promise.all([loadLibrary(), loadPlaylists()]);
  };

  const handleTriggerSync = async () => {
    try {
      await api.triggerSync();
      setStatus((prev) => ({ ...prev, syncing: true }));
    } catch (e: any) {
      setError(e.message || 'Failed to trigger sync');
    }
  };

  if (refreshing && playlists.length === 0) {
    return <Spinner text="Loading playlists…" />;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Dashboard</h2>
        <Button variant="secondary" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Library Stats */}
        <Card>
          <div className="flex items-start justify-between mb-2">
            <div>
              <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider">Library</h3>
              <p className="text-lg font-semibold">{library?.track_count?.toLocaleString() ?? '--'} tracks</p>
            </div>
            <Badge variant="info" dot />
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs text-gray-400 mt-2">
            <span>{library?.album_count?.toLocaleString() ?? '--'} albums</span>
            <span>{library?.artist_count?.toLocaleString() ?? '--'} artists</span>
          </div>
        </Card>

        {/* Sync Status */}
        <Card>
          <div className="flex items-start justify-between mb-2">
            <div>
              <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider">Sync</h3>
              <p className="text-lg font-semibold">{status.syncing ? 'Syncing…' : 'Idle'}</p>
            </div>
            <Badge
              variant={status.syncing ? 'warning' : status.lastSync ? 'success' : 'default'}
              dot
            />
          </div>
          {status.lastSync && (
            <p className="mt-2 text-xs text-gray-400">
              Last sync: {formatDate(status.lastSync)}
            </p>
          )}
          {status.message && (
            <p className="mt-1 text-xs text-gray-400">{status.message}</p>
          )}
          <Button
            variant="accent"
            onClick={handleTriggerSync}
            disabled={status.syncing}
            className="w-full mt-3 text-xs"
          >
            {status.syncing ? 'Syncing…' : 'Sync Now'}
          </Button>
        </Card>

        {/* Playlists */}
        <Card>
          <div className="flex items-start justify-between mb-2">
            <div>
              <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider">Playlists</h3>
              <p className="text-lg font-semibold">{playlists.length}</p>
            </div>
            <Badge variant="success" dot />
          </div>
          <div className="mt-2 space-y-2 text-sm">
            {playlists.map((p) => (
              <div key={p.id} className="flex items-center justify-between px-2 py-1.5 rounded-lg bg-surface-overlay">
                <div className="flex items-center gap-2">
                  <Badge variant="info" dot />
                  <div>
                    <div className="font-medium">{p.name}</div>
                    <div className="text-xs text-gray-400">{p.track_count} tracks</div>
                  </div>
                </div>
                <Badge
                  variant={p.navidrome_playlist_id ? 'success' : 'warning'}
                >
                  {p.navidrome_playlist_id ? 'Synced' : 'Local'}
                </Badge>
              </div>
            ))}
            {playlists.length === 0 && (
              <p className="text-center text-gray-500 text-xs py-2">
                No playlists yet. Create one in the Seed tab.
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="bg-red-900/50 border border-red-500/20 rounded-lg p-4">
          <p className="text-red-400 text-sm">{error}</p>
        </div>
      )}
    </div>
  );
}