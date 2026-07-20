import { useState, useCallback, useEffect } from 'react';
import { api } from '../api';
import { 
  Card, Button, Badge, Input, Slider, formatDuration, 
  SectionHeader, Spinner
} from '../components';
import type { Track, PlaylistGenerateResponse, NavidromePlaylist } from '../types';

const STRICTNESS_LABELS = ['Very Loose', 'Loose', 'Medium', 'Strict', 'Very Strict'];
const PER_SEED_OPTIONS = [3, 5, 8, 12, 20];

type Mode = 'track' | 'playlist';

export function SeedInterface() {
  // Mode
  const [mode, setMode] = useState<Mode>('track');

  // Shared params
  const [strictness, setStrictness] = useState(3);
  const [trackCount, setTrackCount] = useState(20);
  const [generating, setGenerating] = useState(false);
  const [generated, setGenerated] = useState<PlaylistGenerateResponse | null>(null);
  const [pushing, setPushing] = useState(false);
  const [pushResult, setPushResult] = useState<{ success: boolean; message: string } | null>(null);
  const [error, setError] = useState('');

  // Track mode state
  const [search, setSearch] = useState('');
  const [results, setResults] = useState<Track[]>([]);
  const [selectedTrack, setSelectedTrack] = useState<Track | null>(null);
  const [searching, setSearching] = useState(false);

  // Playlist mode state
  const [navidromePlaylists, setNavidromePlaylists] = useState<NavidromePlaylist[]>([]);
  const [loadingPlaylists, setLoadingPlaylists] = useState(false);
  const [selectedPlaylist, setSelectedPlaylist] = useState<NavidromePlaylist | null>(null);
  const [perSeedTrack, setPerSeedTrack] = useState(5);

  // Load Navidrome playlists for playlist mode
  useEffect(() => {
    if (mode === 'playlist' && navidromePlaylists.length === 0 && !loadingPlaylists) {
      loadNavidromePlaylists();
    }
  }, [mode]);

  const loadNavidromePlaylists = useCallback(async () => {
    setLoadingPlaylists(true);
    setError('');
    try {
      const pls = await api.listNavidromePlaylists();
      setNavidromePlaylists(pls);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load Navidrome playlists');
    } finally {
      setLoadingPlaylists(false);
    }
  }, []);

  // Track mode
  const handleSearch = useCallback(async () => {
    if (!search.trim()) return;
    setSearching(true);
    setError('');
    try {
      const tracks = await api.getTracks({ search, limit: 30 });
      setResults(tracks);
      if (tracks.length > 0) setSelectedTrack(null);
      if (tracks.length === 0) setError('No tracks found matching your search');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  }, [search]);

  const handleGenerate = async () => {
    if (mode === 'track' && !selectedTrack) return;
    if (mode === 'playlist' && !selectedPlaylist) return;
    setGenerating(true);
    setError('');
    setGenerated(null);
    setPushResult(null);
    try {
      const result = mode === 'track'
        ? await api.generatePlaylist({
            seed_track_id: selectedTrack!.id,
            track_count: trackCount,
            strictness,
          })
        : await api.generateFromPlaylist({
            navidrome_playlist_id: selectedPlaylist!.id,
            track_count: trackCount,
            strictness,
            per_seed_track: perSeedTrack,
          });
      setGenerated(result);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Generation failed');
    } finally {
      setGenerating(false);
    }
  };

  const handlePush = async () => {
    if (!generated) return;
    setPushing(true);
    setPushResult(null);
    try {
      const result = await api.pushPlaylist({
        name: generated.name,
        track_ids: generated.tracks.map((t) => t.id),
      });
      setPushResult({
        success: true,
        message: `Pushed "${result.name}" to Navidrome (${result.track_count} tracks)`,
      });
    } catch (e: unknown) {
      setPushResult({
        success: false,
        message: e instanceof Error ? e.message : 'Push failed',
      });
    } finally {
      setPushing(false);
    }
  };

  const canGenerate = mode === 'track' ? selectedTrack !== null : selectedPlaylist !== null;

  return (
    <div className="space-y-6">
      <SectionHeader 
        title="Seed-Based Playlist Generation" 
        subtitle={mode === 'track' ? "Create a playlist inspired by your favorite track" : "Create a playlist inspired by an existing Navidrome playlist"}
      />

      {/* Mode Toggle */}
      <div className="flex gap-1 bg-surface-overlay rounded-lg p-1 w-fit">
        <button
          onClick={() => { setMode('track'); setGenerated(null); setError(''); setPushResult(null); }}
          className={`px-4 py-2 rounded-md text-sm font-medium transition-all ${
            mode === 'track'
              ? 'bg-accent text-white shadow-sm'
              : 'text-gray-400 hover:text-white'
          }`}
        >
          Seed Track
        </button>
        <button
          onClick={() => { setMode('playlist'); setGenerated(null); setError(''); setPushResult(null); }}
          className={`px-4 py-2 rounded-md text-sm font-medium transition-all ${
            mode === 'playlist'
              ? 'bg-accent text-white shadow-sm'
              : 'text-gray-400 hover:text-white'
          }`}
        >
          Seed Playlist
        </button>
      </div>

      {/* ─── TRACK MODE ─── */}
      {mode === 'track' && (
        <Card>
          <div className="flex gap-3">
            <div className="flex-1 min-w-0">
              <Input
                value={search}
                onChange={setSearch}
                placeholder="Search for a track by title, artist, or album..."
                onKeyPress={(e) => e.key === 'Enter' && handleSearch()}
              />
            </div>
            <Button 
              onClick={handleSearch} 
              disabled={searching || !search.trim()}
              className="whitespace-nowrap"
            >
              {searching ? (
                <>
                  <Spinner text="" />
                  Searching...
                </>
              ) : (
                'Search'
              )}
            </Button>
          </div>

          {error && mode === 'track' && <p className="mt-2 text-sm text-red-400">{error}</p>}

          {results.length > 0 && (
            <div className="mt-4 max-h-[300px] overflow-y-auto rounded-lg border border-surface-border">
              <table className="w-full text-sm">
                <thead className="bg-surface-overlay/50 sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Title</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Artist</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Album</th>
                    <th className="text-right px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider w-16">Duration</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border/50">
                  {results.map((track) => (
                    <tr
                      key={track.id}
                      onClick={() => setSelectedTrack(track)}
                      className={`cursor-pointer hover:bg-surface-highlight/50 transition-colors duration-100 ${selectedTrack?.id === track.id ? 'bg-accent/10' : ''}`}
                    >
                      <td className="px-3 py-2 font-medium text-white whitespace-nowrap">{track.title}</td>
                      <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{track.artist_name || 'Unknown Artist'}</td>
                      <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{track.album_name || '—'}</td>
                      <td className="px-3 py-2 text-gray-400 text-right whitespace-nowrap tabular-nums">{formatDuration(track.duration)}</td>
                      <td className="px-3 py-2 text-center">
                        {selectedTrack?.id === track.id && (
                          <Badge variant="success" className="px-2 py-0.5 text-xs">Selected</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {/* ─── PLAYLIST MODE ─── */}
      {mode === 'playlist' && (
        <Card>
          <SectionHeader
            title="Select a Seed Playlist"
            subtitle="Choose a Navidrome playlist to use as the source for generation"
            action={
              <Button variant="ghost" onClick={loadNavidromePlaylists} disabled={loadingPlaylists}>
                {loadingPlaylists ? 'Loading...' : 'Refresh'}
              </Button>
            }
          />

          {loadingPlaylists && <Spinner text="Loading Navidrome playlists..." />}

          {error && mode === 'playlist' && (
            <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm mb-4">
              {error}
            </div>
          )}

          {!loadingPlaylists && navidromePlaylists.length === 0 && !error && (
            <div className="text-center py-8 text-sm text-gray-500">
              <div className="text-2xl mb-2">📋</div>
              <p>No Navidrome playlists found.</p>
              <p className="text-xs mt-1">Create some playlists in Navidrome first.</p>
            </div>
          )}

          {navidromePlaylists.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4">
              {navidromePlaylists.map((pl) => (
                <button
                  key={pl.id}
                  onClick={() => setSelectedPlaylist(pl)}
                  className={`text-left p-3 rounded-lg border transition-all ${
                    selectedPlaylist?.id === pl.id
                      ? 'border-accent bg-accent/10 ring-1 ring-accent/30'
                      : 'border-surface-border bg-surface-overlay/50 hover:border-gray-600 hover:bg-surface-highlight/30'
                  }`}
                >
                  <div className="font-medium text-white text-sm truncate">{pl.name}</div>
                  <div className="text-xs text-gray-500 mt-1">{pl.song_count} tracks</div>
                  {selectedPlaylist?.id === pl.id && (
                    <Badge variant="success" className="mt-2">Selected</Badge>
                  )}
                </button>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* ─── PARAMETERS ─── */}
      {canGenerate && (
        <Card>
          <SectionHeader 
            title="Playlist Parameters" 
            subtitle="Customize your generated playlist"
          />
          
          <div className="space-y-4">
            {/* Seed Info */}
            <div className="bg-surface-overlay rounded-lg p-4 border border-surface-border">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-lg bg-accent/20 flex items-center justify-center">
                  <svg className="w-5 h-5 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 8v4l3 3"/>
                  </svg>
                </div>
                <div>
                  <p className="text-xs text-gray-500 mb-1 uppercase tracking-wider font-medium">
                    {mode === 'track' ? 'Seed Track' : 'Seed Playlist'}
                  </p>
                  <p className="text-sm font-medium text-white">
                    {mode === 'track'
                      ? (selectedTrack as Track).title
                      : selectedPlaylist!.name
                    }
                  </p>
                  {mode === 'track' && (
                    <p className="text-xs text-gray-400 mt-0.5">
                      {(selectedTrack as Track).artist_name || 'Unknown Artist'} 
                      {(selectedTrack as Track).album_name ? ` · ${(selectedTrack as Track).album_name}` : ''}
                    </p>
                  )}
                  {mode === 'playlist' && (
                    <p className="text-xs text-gray-400 mt-0.5">
                      {selectedPlaylist!.song_count} seed tracks
                    </p>
                  )}
                </div>
              </div>
            </div>

            {/* Controls */}
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-2">Strictness</label>
                <Slider
                  value={strictness}
                  onChange={setStrictness}
                  label="How closely to match the seed's style"
                  labels={STRICTNESS_LABELS}
                />
              </div>

              {mode === 'playlist' && (
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-2">Similar Tracks Per Seed</label>
                  <div className="flex gap-2">
                    {PER_SEED_OPTIONS.map((n) => (
                      <button
                        key={n}
                        onClick={() => setPerSeedTrack(n)}
                        className={`px-3 py-1.5 rounded-md text-sm transition-all ${
                          perSeedTrack === n
                            ? 'bg-accent text-white'
                            : 'bg-surface-overlay text-gray-400 hover:text-white border border-surface-border'
                        }`}
                      >
                        {n}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-gray-600 mt-1">
                    How many similar songs to fetch per seed track. Higher = more variety, lower = tighter focus.
                  </p>
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-400 mb-2">Track Count</label>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-gray-400 w-16 text-right">5</span>
                  <input
                    type="range"
                    min={5}
                    max={50}
                    value={trackCount}
                    onChange={(e) => setTrackCount(Number(e.target.value))}
                    className="flex-1 h-2 appearance-none cursor-pointer rounded-lg
                      bg-gradient-to-r from-accent via-accent-soft to-accent
                      [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4
                      [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:rounded-full
                      [&::-webkit-slider-thumb]:cursor-pointer [&::-webkit-slider-thumb]:shadow-md
                      [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:h-4
                      [&::-moz-range-thumb]:bg-white [&::-moz-range-thumb]:rounded-full
                      [&::-moz-range-thumb]:cursor-pointer [&::-moz-range-thumb]:border-0"
                  />
                  <span className="text-xs text-gray-400 w-16 text-left">{trackCount}</span>
                </div>
              </div>
            </div>

            <div className="flex justify-end">
              <Button 
                onClick={handleGenerate} 
                disabled={generating}
                className="px-6 py-2.5 text-sm font-medium"
              >
                {generating ? (
                  <>
                    <Spinner text="" />
                    Generating...
                  </>
                ) : (
                  'Generate Playlist'
                )}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* ─── GENERATED PLAYLIST ─── */}
      {generated && (
        <Card>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-bold text-white">{generated.name}</h2>
            <Badge variant="info" className="px-3 py-1.5 text-sm">
              {generated.track_count} tracks
            </Badge>
          </div>

          <div className="overflow-hidden rounded-lg border border-surface-border">
            <table className="w-full text-sm">
              <thead className="bg-surface-overlay/50">
                <tr>
                  <th className="text-left px-4 py-3 text-gray-400 font-medium text-xs uppercase tracking-wider w-8">#</th>
                  <th className="text-left px-4 py-3 text-gray-400 font-medium text-xs uppercase tracking-wider">Track</th>
                  <th className="text-left px-4 py-3 text-gray-400 font-medium text-xs uppercase tracking-wider">Artist</th>
                  <th className="text-left px-4 py-3 text-gray-400 font-medium text-xs uppercase tracking-wider">Album</th>
                  <th className="text-right px-4 py-3 text-gray-400 font-medium text-xs uppercase tracking-wider w-16">Duration</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-border/50">
                {generated.tracks.map((track, index) => (
                  <tr key={track.id} className="hover:bg-surface-highlight/50 transition-colors duration-100">
                    <td className="px-4 py-3 text-gray-500 text-xs tabular-nums whitespace-nowrap">{index + 1}</td>
                    <td className="px-4 py-3 font-medium text-white whitespace-nowrap">{track.title}</td>
                    <td className="px-4 py-3 text-gray-400 whitespace-nowrap">{track.artist_name || 'Unknown Artist'}</td>
                    <td className="px-4 py-3 text-gray-400 whitespace-nowrap">{track.album_name || '—'}</td>
                    <td className="px-4 py-3 text-gray-400 text-right tabular-nums whitespace-nowrap">{formatDuration(track.duration)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex justify-end">
            <Button 
              onClick={handlePush} 
              disabled={pushing}
              className="px-6 py-2.5 text-sm font-medium"
            >
              {pushing ? (
                <>
                  <Spinner text="" />
                  Pushing...
                </>
              ) : (
                'Push to Navidrome'
              )}
            </Button>
          </div>

          {pushResult && (
            <div className="mt-4 px-4 py-3 rounded-lg text-sm font-medium">
              {pushResult.success ? (
                <div className="bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-lg px-4 py-3">
                  {pushResult.message}
                </div>
              ) : (
                <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3">
                  {pushResult.message}
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Global Error */}
      {error && !searching && !generating && !pushing && (
        <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}
    </div>
  );
}
