import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { Card, Button, Badge, Input, Spinner, EmptyState } from '../components';
import type { ConnectionStatus, LibraryStats } from '../types';

export function SourceDashboard() {
  const [config, setConfig] = useState({ url: 'http://192.168.4.205:4533', username: '', password: '' });
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState('');

  const checkStatus = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.connectionStatus();
      setStatus(s);
      if (s.connected) {
        const st = await api.getLibraryStats();
        setStats(st);
      }
    } catch {
      setStatus({ connected: false, message: 'Not configured' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { checkStatus(); }, [checkStatus]);

  const handleConnect = async () => {
    setConnecting(true);
    setError('');
    try {
      const result = await api.connect(config);
      setStatus(result);
      if (result.connected) {
        const st = await api.getLibraryStats();
        setStats(st);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Connection failed');
      setStatus({ connected: false, message: 'Connection failed' });
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Source Management</h2>

      {/* Connection Card */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium">Navidrome Connection</h3>
          {status && (
            <Badge variant={status.connected ? 'success' : 'danger'}>
              {status.connected ? '● Connected' : '● Disconnected'}
            </Badge>
          )}
        </div>

        {status?.server_version && (
          <p className="text-sm text-gray-400 mb-4">Server version: {status.server_version}</p>
        )}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">Server URL</label>
            <Input
              value={config.url}
              onChange={(v) => setConfig((c) => ({ ...c, url: v }))}
              placeholder="http://192.168.4.205:4533"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">Username</label>
            <Input
              value={config.username}
              onChange={(v) => setConfig((c) => ({ ...c, username: v }))}
              placeholder="Username"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">Password</label>
            <Input
              value={config.password}
              onChange={(v) => setConfig((c) => ({ ...c, password: v }))}
              placeholder="Password"
              type="password"
            />
          </div>
        </div>

        {error && <p className="text-sm text-red-400 mb-3">{error}</p>}

        <div className="flex gap-3">
          <Button onClick={handleConnect} disabled={connecting}>
            {connecting ? 'Connecting...' : 'Connect'}
          </Button>
          <Button variant="secondary" onClick={checkStatus} disabled={loading}>
            Refresh
          </Button>
        </div>
      </Card>

      {/* Library Stats */}
      <Card>
        <h3 className="font-medium mb-4">Library Statistics</h3>
        {loading && <Spinner />}
        {stats && (
          <div className="grid grid-cols-3 gap-4">
            <StatBox label="Tracks" value={stats.track_count.toLocaleString()} />
            <StatBox label="Albums" value={stats.album_count.toLocaleString()} />
            <StatBox label="Artists" value={stats.artist_count.toLocaleString()} />
          </div>
        )}
        {!loading && !stats && <EmptyState message="Connect to Navidrome to see library stats" />}
      </Card>
    </div>
  );
}

function StatBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-700/50 rounded-lg p-4 text-center">
      <div className="text-2xl font-bold text-white">{value}</div>
      <div className="text-sm text-gray-400 mt-1">{label}</div>
    </div>
  );
}
