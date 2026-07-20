import { useState } from 'react';
import { api } from './api';

interface LoginViewProps {
  onAuth: () => void;
}

export function LoginView({ onAuth }: LoginViewProps) {
  const [url, setUrl] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const res = await api.login({ url, username, password });
      localStorage.setItem('access_token', res.access_token);
      onAuth();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Connection failed';
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-surface flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <h1 className="text-2xl font-bold text-white mb-1">LocalSync AI</h1>
          <p className="text-sm text-gray-500">Sign in with your Navidrome account</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-xl border border-surface-border bg-surface-raised p-6 space-y-4"
        >
          <div>
            <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">
              Navidrome URL
            </label>
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://music.example.com"
              className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all duration-150"
              required
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="your navidrome username"
              className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all duration-150"
              required
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="your navidrome password"
              className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all duration-150"
              required
              disabled={loading}
            />
          </div>

          {error && (
            <div className="bg-red-500/10 border border-red-500/20 rounded-lg px-3.5 py-2.5 text-sm text-red-400">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-accent hover:bg-accent-soft text-black font-semibold px-4 py-2.5 text-sm transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2"
          >
            {loading && (
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            )}
            {loading ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}