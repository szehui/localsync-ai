import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { Card, Button, Badge, Spinner, EmptyState, formatDate } from '../components';
import type { GeneratedPlaylist } from '../types';

export function PlaylistView() {
  const [playlists, setPlaylists] = useState<GeneratedPlaylist[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadPlaylists = useCallback(async () => {
    setLoading(true);
    try {
      const pls = await api.getGeneratedPlaylists();
      setPlaylists(pls);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load playlists');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadPlaylists(); }, [loadPlaylists]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Generated Playlists</h2>
        <Button variant="secondary" onClick={loadPlaylists} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading && <Spinner />}
      {error && <p className="text-sm text-red-400">{error}</p>}

      {!loading && playlists.length === 0 && (
        <EmptyState message="No playlists generated yet. Go to the Seed tab to create one." />
      )}

      <div className="space-y-4">
        {playlists.map((pl) => (
          <Card key={pl.id}>
            <div className="flex items-start justify-between">
              <div>
                <h3 className="font-medium text-white">{pl.name}</h3>
                <div className="flex items-center gap-3 mt-1 text-sm text-gray-400">
                  <span>{pl.track_count} tracks</span>
                  {pl.seed_track_name && (
                    <>
                      <span>•</span>
                      <span>Seed: {pl.seed_track_name}</span>
                    </>
                  )}
                  <span>•</span>
                  <span>Strictness: {pl.strictness}/5</span>
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  Created: {formatDate(pl.created_at)}
                  {pl.updated_at !== pl.created_at && ` • Updated: ${formatDate(pl.updated_at)}`}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {pl.navidrome_playlist_id ? (
                  <Badge variant="success">On Navidrome</Badge>
                ) : (
                  <Badge variant="warning">Local Only</Badge>
                )}
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
