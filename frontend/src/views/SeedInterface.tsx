import { useState, useCallback } from 'react';
import { api } from '../api';
import { Card, Button, Badge, Input, Slider, formatDuration } from '../components';
import type { Track, PlaylistGenerateResponse } from '../types';

const STRICTNESS_LABELS = ['Very Loose', 'Loose', 'Medium', 'Strict', 'Very Strict'];

export function SeedInterface() {
  const [search, setSearch] = useState('');
  const [results, setResults] = useState<Track[]>([]);
  const [selectedTrack, setSelectedTrack] = useState<Track | null>(null);
  const [strictness, setStrictness] = useState(3);
  const [trackCount, setTrackCount] = useState(20);
  const [generating, setGenerating] = useState(false);
  const [generated, setGenerated] = useState<PlaylistGenerateResponse | null>(null);
  const [pushing, setPushing] = useState(false);
  const [pushResult, setPushResult] = useState<{ success: boolean; message: string } | null>(null);
  const [error, setError] = useState('');
  const [searching, setSearching] = useState(false);

  const handleSearch = useCallback(async () => {
    if (!search.trim()) return;
    setSearching(true);
    setError('');
    try {
      const tracks = await api.getTracks({ search, limit: 30 });
      setResults(tracks);
      if (tracks.length === 0) {
        setError('No tracks found matching your search');
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  }, [search]);

  const handleGenerate = async () => {
    if (!selectedTrack) return;
    setGenerating(true);
    setError('');
    setGenerated(null);
    setPushResult(null);
    try {
      const result = await api.generatePlaylist({
        seed_track_id: selectedTrack.id,
        track_count: trackCount,
        strictness,
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

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Seed-Based Playlist Generation</h2>

      {/* Search */}
      <Card>
        <h3 className="font-medium mb-3">Find a Seed Track</h3>
        <div className="flex gap-3">
          <div className="flex-1">
            <Input
              value={search}
              onChange={setSearch}
              placeholder="Search by title, artist, or album..."
            />
          </div>
          <Button onClick={handleSearch} disabled={searching || !search.trim()}>
            {searching ? 'Searching...' : 'Search'}
          </Button>
        </div>

        {/* Search Results */}
        {results.length > 0 && (
          <div className="mt-4 max-h-64 overflow-y-auto rounded-md border border-gray-700">
            <table className="w-full text-sm">
              <thead className="bg-gray-700/50 sticky top-0">
                <tr>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Title</th>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Artist</th>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Album</th>
                  <th className="text-right px-3 py-2 text-gray-400 font-medium">Duration</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {results.map((track) => (
                  <tr
                    key={track.id}
                    className={`border-t border-gray-700/50 cursor-pointer hover:bg-gray-700/30 ${
                      selectedTrack?.id === track.id ? 'bg-blue-900/20' : ''
                    }`}
                    onClick={() => setSelectedTrack(track)}
                  >
                    <td className="px-3 py-2 font-medium">{track.title}</td>
                    <td className="px-3 py-2 text-gray-400">{track.artist_name || 'Unknown'}</td>
                    <td className="px-3 py-2 text-gray-400">{track.album_name || '—'}</td>
                    <td className="px-3 py-2 text-gray-400 text-right">{formatDuration(track.duration)}</td>
                    <td className="px-3 py-2">
                      {selectedTrack?.id === track.id && <Badge variant="success">Selected</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Parameters */}
      {selectedTrack && (
        <Card>
          <h3 className="font-medium mb-1">Playlist Parameters</h3>
          <p className="text-sm text-gray-400 mb-4">
            Seed: <span className="text-white font-medium">{selectedTrack.title}</span>
            {' by '}
            <span className="text-white">{selectedTrack.artist_name || 'Unknown'}</span>
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Slider
              value={strictness}
              onChange={setStrictness}
              label="Strictness"
              labels={STRICTNESS_LABELS}
            />
            <div>
              <label className="block text-sm text-gray-400 mb-1">Track Count: {trackCount}</label>
              <input
                type="range"
                min={5}
                max={50}
                value={trackCount}
                onChange={(e) => setTrackCount(Number(e.target.value))}
                className="w-full h-2 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-blue-500"
              />
              <div className="flex justify-between mt-1">
                <span className="text-xs text-gray-500">5</span>
                <span className="text-xs text-gray-500">50</span>
              </div>
            </div>
          </div>

          <div className="mt-4">
            <Button onClick={handleGenerate} disabled={generating}>
              {generating ? 'Generating...' : 'Generate Playlist'}
            </Button>
          </div>
        </Card>
      )}

      {/* Generated Results */}
      {generated && (
        <Card>
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium">{generated.name}</h3>
            <Badge>{generated.track_count} tracks</Badge>
          </div>

          <div className="max-h-72 overflow-y-auto rounded-md border border-gray-700 mb-4">
            <table className="w-full text-sm">
              <thead className="bg-gray-700/50 sticky top-0">
                <tr>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">#</th>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Title</th>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Artist</th>
                  <th className="text-left px-3 py-2 text-gray-400 font-medium">Album</th>
                  <th className="text-right px-3 py-2 text-gray-400 font-medium">Duration</th>
                </tr>
              </thead>
              <tbody>
                {generated.tracks.map((track, i) => (
                  <tr key={track.id} className="border-t border-gray-700/50 hover:bg-gray-700/30">
                    <td className="px-3 py-2 text-gray-500">{i + 1}</td>
                    <td className="px-3 py-2 font-medium">{track.title}</td>
                    <td className="px-3 py-2 text-gray-400">{track.artist_name || 'Unknown'}</td>
                    <td className="px-3 py-2 text-gray-400">{track.album_name || '—'}</td>
                    <td className="px-3 py-2 text-gray-400 text-right">{formatDuration(track.duration)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <Button onClick={handlePush} disabled={pushing}>
            {pushing ? 'Pushing...' : 'Push to Navidrome'}
          </Button>

          {pushResult && (
            <p className={`mt-3 text-sm ${pushResult.success ? 'text-green-400' : 'text-red-400'}`}>
              {pushResult.message}
            </p>
          )}
        </Card>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
