import { useState } from 'react';
import { SourceDashboard } from './views/SourceDashboard.tsx';
import { SeedInterface } from './views/SeedInterface.tsx';
import { PlaylistView } from './views/PlaylistView.tsx';
import { AutomationHub } from './views/AutomationHub.tsx';

type Tab = 'source' | 'seed' | 'playlists' | 'automation';

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: 'source', label: 'Source', icon: '🎵' },
  { id: 'seed', label: 'Seed', icon: '🌱' },
  { id: 'playlists', label: 'Playlists', icon: '📋' },
  { id: 'automation', label: 'Automation', icon: '⚡' },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('source');

  return (
    <div className="min-h-screen bg-gray-900 text-white flex flex-col">
      {/* Header */}
      <header className="border-b border-gray-700 bg-gray-800/50 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold tracking-tight">LocalSync AI</h1>
            <p className="text-xs text-gray-400">Music discovery for Navidrome</p>
          </div>
          <ConnectionIndicator />
        </div>
        {/* Tabs */}
        <div className="max-w-6xl mx-auto px-4 flex gap-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-2 text-sm font-medium rounded-t-md transition-colors ${
                activeTab === tab.id
                  ? 'bg-gray-900 text-white border-t border-x border-gray-700'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
              }`}
            >
              <span className="mr-1.5">{tab.icon}</span>
              {tab.label}
            </button>
          ))}
        </div>
      </header>

      {/* Content */}
      <main className="flex-1 max-w-6xl mx-auto w-full px-4 py-6">
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

  // Check connection on mount
  useState(() => {
    import('./api.ts')
      .then(({ api }) => api.connectionStatus())
      .then((s) => setStatus(s.connected ? 'connected' : 'disconnected'))
      .catch(() => setStatus('disconnected'));
  });

  const config = {
    checking: { color: 'bg-yellow-500', text: 'Checking...' },
    connected: { color: 'bg-green-500', text: 'Connected' },
    disconnected: { color: 'bg-red-500', text: 'Disconnected' },
  }[status];

  return (
    <div className="flex items-center gap-2 text-xs">
      <div className={`w-2 h-2 rounded-full ${config.color}`} />
      <span className="text-gray-400">{config.text}</span>
    </div>
  );
}
