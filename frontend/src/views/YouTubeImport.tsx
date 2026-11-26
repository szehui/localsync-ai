import { useState, useCallback, useEffect } from 'react';
import { api } from '../api';
import { Button, Input, Card, Badge, Spinner, SectionHeader } from '../components';

interface YouTubeTrack {
  title: string;
  artist: string;
  album: string;
  duration: number;
  youtube_id: string;
}

interface TrackMatchResult {
  title: string;
  artist: string;
  album: string;
  status: 'matched' | 'missing' | 'queued_to_lidarr';
  lidarr_album_added: boolean;
  lidarr_artist_added: boolean;
  error?: string;
}

interface YouTubeParseResponse {
  playlist_title: string;
  track_count: number;
  tracks: YouTubeTrack[];
}

interface YouTubeImportResult {
  playlist_name: string;
  navidrome_playlist_id?: string;
  matched_count: number;
  missing_count: number;
  lidarr_queued_count: number;
  lidarr_albums: string[];
  lidarr_artists: string[];
  tracks: TrackMatchResult[];
}

interface LidarrConfigResponse {
  configured: boolean;
  url?: string;
  connected?: boolean;
  version?: string;
}

export function YouTubeImport() {
  // Parse state
  const [url, setUrl] = useState('');
  const [parsing, setParsing] = useState(false);
  const [parsedPlaylist, setParsedPlaylist] = useState<YouTubeParseResponse | null>(null);
  const [parseError, setParseError] = useState('');

  // Import state
  const [playlistName, setPlaylistName] = useState('');
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<YouTubeImportResult | null>(null);
  const [importError, setImportError] = useState('');

  // Lidarr config state
  const [lidarrConfig, setLidarrConfig] = useState<LidarrConfigResponse | null>(null);
  const [lidarrUrl, setLidarrUrl] = useState('');
  const [lidarrApiKey, setLidarrApiKey] = useState('');
  const [savingLidarr, setSavingLidarr] = useState(false);
  const [lidarrMessage, setLidarrMessage] = useState('');

  // Load Lidarr config on mount
  useEffect(() => {
    loadLidarrConfig();
  }, []);

  const loadLidarrConfig = useCallback(async () => {
    try {
      const config = await api.getLidarrConfig();
      setLidarrConfig(config);
      if (config.url) {
        setLidarrUrl(config.url);
      }
    } catch (e) {
      console.error('Failed to load Lidarr config:', e);
    }
  }, []);

  const handleSaveLidarr = useCallback(async () => {
    if (!lidarrUrl || !lidarrApiKey) {
      setLidarrMessage('Please enter both URL and API key');
      return;
    }

    setSavingLidarr(true);
    setLidarrMessage('');
    try {
      const result = await api.setLidarrConfig({ url: lidarrUrl, api_key: lidarrApiKey });
      setLidarrConfig(result);
      setLidarrMessage(`Connected to Lidarr v${result.version}`);
    } catch (e: unknown) {
      setLidarrMessage(e instanceof Error ? e.message : 'Failed to connect to Lidarr');
    } finally {
      setSavingLidarr(false);
    }
  }, [lidarrUrl, lidarrApiKey]);

  const handleClearLidarr = useCallback(async () => {
    setSavingLidarr(true);
    try {
      await api.clearLidarrConfig();
      setLidarrConfig(null);
      setLidarrUrl('');
      setLidarrApiKey('');
      setLidarrMessage('Lidarr configuration cleared');
    } catch (e: unknown) {
      setLidarrMessage(e instanceof Error ? e.message : 'Failed to clear config');
    } finally {
      setSavingLidarr(false);
    }
  }, []);

  const handleParse = useCallback(async () => {
    if (!url.trim()) {
      setParseError('Please enter a YouTube Music playlist URL');
      return;
    }

    setParsing(true);
    setParseError('');
    setParsedPlaylist(null);
    setImportResult(null);
    setPlaylistName('');

    try {
      const result = await api.parseYouTubePlaylist({ url: url.trim() });
      setParsedPlaylist(result);
      setPlaylistName(result.playlist_title);
    } catch (e: unknown) {
      setParseError(e instanceof Error ? e.message : 'Failed to parse playlist');
    } finally {
      setParsing(false);
    }
  }, [url]);

  const handleImport = useCallback(async () => {
    if (!parsedPlaylist) return;

    setImporting(true);
    setImportError('');
    setImportResult(null);

    try {
      const result = await api.importYouTubePlaylist({
        url: url.trim(),
        playlist_name: playlistName || undefined,
      });
      setImportResult(result);
    } catch (e: unknown) {
      setImportError(e instanceof Error ? e.message : 'Failed to import playlist');
    } finally {
      setImporting(false);
    }
  }, [parsedPlaylist, url, playlistName]);

  return (
    <div className="space-y-6">
      <SectionHeader
        title="YouTube Music Import"
        subtitle="Import playlists from YouTube Music to Navidrome"
      />

      {/* Lidarr Configuration (collapsible) */}
      <Card>
        <details className="group">
          <summary className="cursor-pointer flex items-center justify-between">
            <div className="flex items-center gap-2">
              <h3 className="font-medium text-white">Lidarr Integration</h3>
              {lidarrConfig?.configured ? (
                <Badge variant={lidarrConfig.connected ? 'success' : 'warning'} dot>
                  {lidarrConfig.connected ? 'Connected' : 'Disconnected'}
                </Badge>
              ) : (
                <Badge variant="default">Not Configured</Badge>
              )}
            </div>
            <span className="text-gray-500 group-open:rotate-180 transition-transform">▾</span>
          </summary>

          <div className="mt-4 space-y-4">
            <p className="text-sm text-gray-400">
              Configure Lidarr to automatically add missing artists for download.
            </p>

            {lidarrConfig?.configured && (
              <div className="bg-surface-overlay rounded-lg p-3 text-sm">
                <div className="text-gray-400">
                  <span className="font-medium">URL:</span> {lidarrConfig.url}
                  {lidarrConfig.version && (
                    <span className="ml-2 text-gray-500">v{lidarrConfig.version}</span>
                  )}
                </div>
              </div>
            )}

            <div className="space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Lidarr URL</label>
                <Input
                  value={lidarrUrl}
                  onChange={setLidarrUrl}
                  placeholder="http://localhost:8686"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">API Key</label>
                <Input
                  value={lidarrApiKey}
                  onChange={setLidarrApiKey}
                  placeholder="Your Lidarr API key"
                  type="password"
                />
              </div>
            </div>

            <div className="flex gap-2">
              <Button
                onClick={handleSaveLidarr}
                disabled={savingLidarr || !lidarrUrl || !lidarrApiKey}
                className="px-4 py-2 text-sm"
              >
                {savingLidarr ? (
                  <>
                    <Spinner text="" />
                    Saving...
                  </>
                ) : (
                  'Save & Test'
                )}
              </Button>
              {lidarrConfig?.configured && (
                <Button
                  onClick={handleClearLidarr}
                  disabled={savingLidarr}
                  variant="ghost"
                  className="px-4 py-2 text-sm"
                >
                  Clear
                </Button>
              )}
            </div>

            {lidarrMessage && (
              <div className={`text-sm ${lidarrMessage.includes('Connected') || lidarrMessage.includes('cleared') ? 'text-emerald-400' : 'text-red-400'}`}>
                {lidarrMessage}
              </div>
            )}
          </div>
        </details>
      </Card>

      {/* URL Input */}
      <Card>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-2">
              YouTube Music Playlist URL
            </label>
            <div className="flex gap-3">
              <div className="flex-1">
                <Input
                  value={url}
                  onChange={setUrl}
                  placeholder="https://music.youtube.com/playlist?list=PLxxxxxx"
                  onKeyPress={(e) => e.key === 'Enter' && handleParse()}
                />
              </div>
              <Button
                onClick={handleParse}
                disabled={parsing || !url.trim()}
                className="whitespace-nowrap"
              >
                {parsing ? (
                  <>
                    <Spinner text="" />
                    Parsing...
                  </>
                ) : (
                  'Parse Playlist'
                )}
              </Button>
            </div>
          </div>

          {parseError && (
            <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm">
              {parseError}
            </div>
          )}
        </div>
      </Card>

      {/* Parsed Playlist Preview */}
      {parsedPlaylist && (
        <Card>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-medium text-white">{parsedPlaylist.playlist_title}</h3>
                <p className="text-sm text-gray-400">
                  {parsedPlaylist.track_count} tracks found
                </p>
              </div>
              <Badge variant="info">{parsedPlaylist.track_count} tracks</Badge>
            </div>

            {/* Track list preview (first 10) */}
            <div className="max-h-[300px] overflow-y-auto rounded-lg border border-surface-border">
              <table className="w-full text-sm">
                <thead className="bg-surface-overlay/50 sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider w-8">#</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Title</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Artist</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Album</th>
                    <th className="text-right px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider w-16">Duration</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border/50">
                  {parsedPlaylist.tracks.slice(0, 10).map((track, idx) => (
                    <tr key={track.youtube_id || idx} className="hover:bg-surface-highlight/50">
                      <td className="px-3 py-2 text-gray-500 text-xs tabular-nums">{idx + 1}</td>
                      <td className="px-3 py-2 font-medium text-white whitespace-nowrap">{track.title}</td>
                      <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{track.artist || 'Unknown Artist'}</td>
                      <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{track.album || '—'}</td>
                      <td className="px-3 py-2 text-gray-400 text-right whitespace-nowrap tabular-nums">
                        {formatDuration(track.duration)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {parsedPlaylist.track_count > 10 && (
              <p className="text-xs text-gray-500 text-center">
                Showing first 10 of {parsedPlaylist.track_count} tracks
              </p>
            )}

            {/* Import options */}
            <div className="space-y-3 pt-2">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">
                  Playlist Name (optional)
                </label>
                <Input
                  value={playlistName}
                  onChange={setPlaylistName}
                  placeholder="Custom playlist name"
                />
              </div>

              <Button
                onClick={handleImport}
                disabled={importing}
                className="w-full px-6 py-2.5 text-sm font-medium"
              >
                {importing ? (
                  <>
                    <Spinner text="" />
                    Importing...
                  </>
                ) : (
                  'Import to Navidrome'
                )}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* Import Results */}
      {importResult && (
        <Card>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="font-medium text-white">Import Complete</h3>
              {importResult.navidrome_playlist_id && (
                <Badge variant="success" dot>Playlist Created</Badge>
              )}
            </div>

            <div className="grid grid-cols-3 gap-4 text-center">
              <div className="bg-surface-overlay rounded-lg p-4">
                <div className="text-2xl font-bold text-emerald-400">{importResult.matched_count}</div>
                <div className="text-xs text-gray-500">Matched in Library</div>
              </div>
              <div className="bg-surface-overlay rounded-lg p-4">
                <div className="text-2xl font-bold text-amber-400">{importResult.missing_count}</div>
                <div className="text-xs text-gray-500">Missing</div>
              </div>
              <div className="bg-surface-overlay rounded-lg p-4">
                <div className="text-2xl font-bold text-blue-400">{importResult.lidarr_queued_count}</div>
                <div className="text-xs text-gray-500">Albums queued</div>
              </div>
            </div>

            {/* Track results */}
            <div className="max-h-[400px] overflow-y-auto rounded-lg border border-surface-border">
              <table className="w-full text-sm">
                <thead className="bg-surface-overlay/50 sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider w-6">#</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Title</th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider">Artist</th>
                    <th className="text-center px-3 py-2 text-gray-400 font-medium text-xs uppercase tracking-wider w-24">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border/50">
                  {importResult.tracks.map((track, idx) => (
                    <tr key={idx} className="hover:bg-surface-highlight/50">
                      <td className="px-3 py-2 text-gray-500 text-xs tabular-nums">{idx + 1}</td>
                      <td className="px-3 py-2 font-medium text-white whitespace-nowrap">{track.title}</td>
                      <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{track.artist || 'Unknown Artist'}</td>
                      <td className="px-3 py-2 text-center">
                        {track.status === 'matched' && (
                          <Badge variant="success">✅ Found</Badge>
                        )}
                        {track.status === 'missing' && !track.lidarr_album_added && (
                          <Badge variant="warning">❌ Missing</Badge>
                        )}
                        {track.lidarr_album_added && (
                          <Badge variant="info">📥 Album queued</Badge>
                        )}
                        {track.lidarr_artist_added && (
                          <Badge variant="info">📥 Artist queued</Badge>
                        )}
                        {track.error && (
                          <Badge variant="danger">⚠️ Error</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {importResult.lidarr_queued_count > 0 && !lidarrConfig?.configured && (
              <div className="bg-amber-500/10 text-amber-400 border border-amber-500/20 rounded-lg px-4 py-3 text-sm">
                Lidarr is not configured. Artists were found but not added. Configure Lidarr above to enable automatic artist tracking.
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Global Error */}
      {importError && (
        <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm">
          {importError}
        </div>
      )}
    </div>
  );
}

function formatDuration(seconds: number): string {
  if (!seconds) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${String(secs).padStart(2, '0')}`;
}