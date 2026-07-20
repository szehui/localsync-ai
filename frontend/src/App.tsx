import { useState, useEffect } from 'react';
import { AuthProvider, useAuth } from './AuthContext.tsx';
import { LoginView } from './LoginView.tsx';
import { SourceDashboard } from './views/SourceDashboard.tsx';
import { SeedInterface } from './views/SeedInterface.tsx';
import { PlaylistView } from './views/PlaylistView.tsx';
import { AutomationHub } from './views/AutomationHub.tsx';

type Tab = 'source' | 'seed' | 'playlists' | 'automation';

const TABS: { id: Tab; label: string; icon: string; description: string }[] = [
  { id: 'source', label: 'Source', icon: '🎵', description: 'Connect & sync your library' },
  { id: 'seed', label: 'Seed', icon: '🌱', description: 'Generate from any track' },
  { id: 'playlists', label: 'Playlists', icon: '📋', description: 'View and manage' },
  { id: 'automation', label: 'Automation', icon: '⚡', description: 'Smart triggers' },
];

export default function App() {
  return (
    <AuthProvider>
      <AppRouter />
    </AuthProvider>
  );
}

function AppRouter() {
  const { authenticated, loading, login } = useAuth();

  if (loading) {
    return (
      <div className="min-h-screen bg-surface flex items-center justify-center">
        <div className="flex items-center gap-3 text-gray-400">
          <svg className="animate-spin h-5 w-5 text-accent" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <span className="text-sm">Checking session...</span>
        </div>
      </div>
    );
  }

  if (!authenticated) {
    return <LoginView onAuth={() => { /* login() was already called inside LoginView */ }} />;
  }

  return <MainApp />;
}

function MainApp() {
  const [activeTab, setActiveTab] = useState<Tab>('source');
  const { user, logout } = useAuth();

  return (
    <div className="min-h-screen bg-surface text-white flex flex-col">
      {/* Header */}
      <header className="border-b border-surface-border bg-surface/80 backdrop-blur-xl sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent to-accent-soft flex items-center justify-center shadow-lg shadow-accent/20">
                <svg className="w-4 h-4 text-black" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/>
                </svg>
              </div>
              <div>
                <h1 className="text-base font-bold tracking-tight text-white">LocalSync AI</h1>
                <p className="text-[11px] text-gray-500 -mt-0.5">Music discovery for Navidrome</p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <ConnectionIndicator />
              {user && (
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <span className="hidden sm:inline">{user.username}</span>
                  <button
                    onClick={logout}
                    className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
                  >
                    Logout
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
        {/* Tabs */}
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
          <nav className="flex gap-1 -mb-px">
            {TABS.map((tab) => {
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`
                    group relative px-4 py-3 text-sm font-medium transition-all duration-200
                    ${isActive
                      ? 'text-accent'
                      : 'text-gray-500 hover:text-gray-300'
                    }
                  `}
                  title={tab.description}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-base">{tab.icon}</span>
                    <span className="hidden sm:inline">{tab.label}</span>
                  </div>
                  {/* Active indicator */}
                  {isActive && (
                    <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-accent to-accent-soft rounded-full" />
                  )}
                </button>
              );
            })}
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className="flex-1 max-w-6xl mx-auto w-full px-4 sm:px-6 lg:px-8 py-8 animate-fade-in">
        {activeTab === 'source' && <SourceDashboard />}
        {activeTab === 'seed' && <SeedInterface />}
        {activeTab === 'playlists' && <PlaylistView />}
        {activeTab === 'automation' && <AutomationHub />}
      </main>
    </div>
  );
}

function ConnectionIndicator() {
  const [status, setStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');

  useEffect(() => {
    import('./api.ts')
      .then(({ api }) => api.connectionStatus())
      .then((s) => setStatus(s.connected ? 'connected' : 'disconnected'))
      .catch(() => setStatus('disconnected'));
  }, []);

  const config = {
    checking: { color: 'bg-amber-400', text: 'Checking...', label: 'checking connection' },
    connected: { color: 'bg-emerald-400', text: 'Connected', label: 'connected to Navidrome' },
    disconnected: { color: 'bg-red-400', text: 'Disconnected', label: 'not connected' },
  }[status];

  return (
    <div className="flex items-center gap-2 text-xs" title={config.label}>
      <span className={`w-2 h-2 rounded-full ${config.color} shadow-sm ${status === 'checking' ? 'animate-pulse-soft' : ''}`} />
      <span className="text-gray-500 font-medium hidden sm:inline">{config.text}</span>
    </div>
  );
}