import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { Button, Badge, Spinner, EmptyState, formatDate, SectionHeader } from '../components';
import type { GeneratedPlaylist, Track } from '../types';

export function PlaylistView() {
  const [playlists, setPlaylists] = useState<GeneratedPlaylist[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [tracksMap, setTracksMap] = useState<Record<number, Track[]>>({});
  const [loadingTracks, setLoadingTracks] = useState<number | null>(null);

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

  const toggleExpand = async (plId: number) => {
    if (expandedId === plId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(plId);
    if (!tracksMap[plId]) {
      setLoadingTracks(plId);
      try {
        const tracks = await api.getPlaylistTracks(plId);
        setTracksMap(prev => ({ ...prev, [plId]: tracks }));
      } catch {
        setTracksMap(prev => ({ ...prev, [plId]: [] }));
      } finally {
        setLoadingTracks(null);
      }
    }
  };

  const grouped = playlists.reduce((acc, pl) => {
    const month = pl.created_at?.slice(0, 7) || 'unknown';
    if (!acc[month]) acc[month] = [];
    acc[month].push(pl);
    return acc;
  }, {} as Record<string, GeneratedPlaylist[]>);

  const months = Object.keys(grouped).sort().reverse();

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Generated Playlists"
        subtitle={`${playlists.length} playlist${playlists.length !== 1 ? 's' : ''} created`}
        action={
          <Button variant="secondary" onClick={loadPlaylists} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </Button>
        }
      />

      {loading && <Spinner text="Loading playlists…" />}
      {error && (
        <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {!loading && playlists.length === 0 && (
        <EmptyState
          message="No playlists generated yet. Go to the Seed tab to create one."
          icon="🎵"
        />
      )}

      {!loading && months.map((month) => (
        <div key={month}>
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">
            {new Date(month + '-01').toLocaleDateString('en-US', { year: 'numeric', month: 'long' })}
          </h3>
          <div className="space-y-1">
            {grouped[month].map((pl) => {
              const status = pl.navidrome_playlist_id ? 'synced' : 'local';
              const isExpanded = expandedId === pl.id;
              const tracks = tracksMap[pl.id];
              const isLoading = loadingTracks === pl.id;

              let seedLabel = '';
              if (pl.seed_playlist_name) {
                seedLabel = `Playlist: ${pl.seed_playlist_name}`;
              } else if (pl.seed_track_name) {
                seedLabel = pl.seed_track_name;
              }

              return (
                <div
                  key={pl.id}
                  onClick={() => toggleExpand(pl.id)}
                  className={`rounded-xl border border-surface-border bg-surface-raised p-5 transition-all duration-200 cursor-pointer hover:border-surface-highlight hover:shadow-lg hover:shadow-black/10 ${
                    isExpanded ? 'ring-1 ring-accent/30' : ''
                  }`}
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <div className={`
                          w-2 h-2 rounded-full flex-shrink-0
                          ${status === 'synced' ? 'bg-emerald-400 shadow-sm shadow-emerald-400/30' : 'bg-amber-400 shadow-sm shadow-amber-400/30'}
                        `} />
                        <h3 className="font-medium text-white truncate">{pl.name}</h3>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-xs text-gray-500">
                        <span className="tabular-nums">{pl.track_count} tracks</span>
                        {seedLabel && (
                          <>
                            <span className="text-gray-600">|</span>
                            <span>Seed: <span className="text-gray-400">{seedLabel}</span></span>
                          </>
                        )}
                        <span className="text-gray-600">|</span>
                        <span>Strictness: {pl.strictness}/5</span>
                      </div>
                      <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-600">
                        <span>Created: {formatDate(pl.created_at)}</span>
                        {pl.updated_at !== pl.created_at && (
                          <>
                            <span>·</span>
                            <span>Updated: {formatDate(pl.updated_at)}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <div className="flex-shrink-0 flex items-center gap-2">
                      <Badge
                        variant={status === 'synced' ? 'success' : 'warning'}
                        dot
                      >
                        {status === 'synced' ? 'On Navidrome' : 'Local Only'}
                      </Badge>
                      <span className={`text-gray-600 transition-transform ${isExpanded ? 'rotate-180' : ''}`}>
                        ▾
                      </span>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="mt-3 pt-3 border-t border-surface-border/50">
                      {isLoading ? (
                        <div className="py-4 flex items-center justify-center">
                          <Spinner text="Loading tracks…" />
                        </div>
                      ) : tracks && tracks.length > 0 ? (
                        <div className="space-y-0.5">
                          <div className="flex items-center gap-3 px-3 py-1.5 text-xs text-gray-600 font-medium">
                            <span className="w-6 flex-shrink-0 text-right">#</span>
                            <span className="flex-1">Title</span>
                            <span className="flex-shrink-0">Duration</span>
                          </div>
                          {tracks.map((track, idx) => (
                            <div key={track.id} className="flex items-center gap-3 px-3 py-1.5 hover:bg-surface-highlight/40 rounded-md transition-colors group">
                              <span className="text-xs text-gray-600 w-6 text-right tabular-nums flex-shrink-0">{idx + 1}</span>
                              <div className="flex-1 min-w-0">
                                <div className="text-sm text-white truncate">{track.title}</div>
                                <div className="text-xs text-gray-500 truncate">
                                  {track.artist_name}{track.artist_name && track.album_name ? ' · ' : ''}{track.album_name}
                                </div>
                              </div>
                              <div className="flex items-center gap-2 text-xs text-gray-600 flex-shrink-0">
                                {track.genre && (
                                  <Badge variant="info">{track.genre}</Badge>
                                )}
                                {track.duration !== undefined && track.duration !== null && (
                                  <span className="tabular-nums">{Math.floor(track.duration / 60)}:{String(track.duration % 60).padStart(2, '0')}</span>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="py-4 text-center text-sm text-gray-500">
                          No track data available for this playlist.
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
