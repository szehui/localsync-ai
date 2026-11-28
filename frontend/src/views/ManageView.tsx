/**
 * Owner-only settings: the household webhook and the blocklist.
 *
 * Two rules from the server shape this view:
 *
 * - The webhook URL is write-only. The API returns a masked hint and never the
 *   token, so the field is left **empty** with the stored hint shown beside it.
 *   Typing a new URL replaces it; leaving it blank keeps the stored one. A view
 *   that pre-filled the mask would save the mask as the webhook.
 * - A block is enforced server-side, so this panel is for *review*, not for
 *   safety: adding or removing one changes what search offers next time.
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.ts';
import type {
  BlocklistEntry,
  BlocklistScope,
  NotificationConfig,
  NotificationEvent,
} from '../types.ts';
import {
  Badge,
  Button,
  Card,
  EmptyState,
  SectionHeader,
  Spinner,
  formatRelativeTime,
} from '../components.tsx';

const EVENT_LABELS: Record<NotificationEvent, { title: string; hint: string }> = {
  'request.created': {
    title: 'New requests',
    hint: 'Someone in the household asks for an album',
  },
  'request.available': {
    title: 'Albums arriving',
    hint: 'An album lands in Navidrome — once per request, not per track',
  },
  'request.failed': {
    title: 'Failures',
    hint: 'Lidarr gave up on something — worth a look',
  },
};

export function ManageView() {
  return (
    <div className="space-y-8">
      <NotificationsPanel />
      <BlocklistPanel />
    </div>
  );
}

// ── Notifications ───────────────────────────────────────────────────────────

function NotificationsPanel() {
  const [config, setConfig] = useState<NotificationConfig | null>(null);
  const [url, setUrl] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [events, setEvents] = useState<NotificationEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getNotificationConfig();
      setConfig(data);
      // Configuring a webhook for the first time means you want it on: the
      // server's `enabled: false` for an absent row is a storage detail, not a
      // preference, and echoing it back made "Save" silently store a disabled
      // webhook that only the test button could reach.
      setEnabled(data.configured ? data.enabled : true);
      setEvents(data.events);
      setBaseUrl(data.base_url ?? '');
      // Deliberately blank: the real URL is never sent to the browser.
      setUrl('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const payload = {
        enabled,
        events,
        base_url: baseUrl.trim() || undefined,
        // Only sent when the owner typed something; omitting it keeps the
        // stored webhook rather than overwriting it with an empty string.
        ...(url.trim() ? { webhook_url: url.trim() } : {}),
      };
      const data = await api.setNotificationConfig(payload);
      setConfig(data);
      setUrl('');
      setNotice('Saved.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await api.testNotification();
      if (result.sent) setNotice(result.detail);
      else setError(result.detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const data = await api.clearNotificationConfig();
      setConfig(data);
      setUrl('');
      setBaseUrl('');
      setEvents(data.available_events);
      setNotice('Webhook removed.');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleEvent(name: NotificationEvent) {
    setEvents((current) =>
      current.includes(name)
        ? current.filter((e) => e !== name)
        : [...current, name],
    );
  }

  if (loading) return <Spinner text="Loading notification settings…" />;

  const available = config?.available_events ?? Object.keys(EVENT_LABELS) as NotificationEvent[];

  return (
    <div className="space-y-4">
      <SectionHeader
        title="Notifications"
        subtitle="A Discord webhook that tells the household what happened"
      />

      {notice && (
        <Card className="p-3 border border-emerald-500/30">
          <p className="text-xs text-emerald-300">{notice}</p>
        </Card>
      )}
      {error && (
        <Card className="p-3 border border-red-500/30">
          <p className="text-xs text-red-400">{error}</p>
        </Card>
      )}

      <Card className="p-4 space-y-4">
        <div>
          <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">
            Discord webhook URL
          </label>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={config?.configured
              ? 'Stored — type a new URL to replace it'
              : 'https://discord.com/api/webhooks/…'}
            className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all"
          />
          {config?.webhook_hint && (
            <p className="mt-1.5 text-[11px] text-gray-500">
              Currently pointing at <span className="text-gray-400 font-mono">{config.webhook_hint}</span>
              {' '}— the token is never shown. Leave the field blank to keep it.
            </p>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">
            Link base URL <span className="normal-case text-gray-600">(optional)</span>
          </label>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://localsync.example.org"
            className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all"
          />
          <p className="mt-1.5 text-[11px] text-gray-500">
            Used to link an embed back to this app. The background reconciler has
            no request to borrow a hostname from, so set it if you want links.
          </p>
        </div>

        <div>
          <p className="text-xs font-medium text-gray-400 mb-2 tracking-wide uppercase">
            Which events
          </p>
          <div className="space-y-2">
            {available.map((name) => {
              const meta = EVENT_LABELS[name];
              const on = events.includes(name);
              return (
                <label
                  key={name}
                  className="flex items-start gap-3 rounded-lg border border-surface-border px-3 py-2 cursor-pointer hover:border-gray-600 transition-colors"
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => toggleEvent(name)}
                    className="mt-0.5 accent-current text-accent"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm text-white">{meta?.title ?? name}</span>
                    <span className="block text-[11px] text-gray-500">
                      {meta?.hint ?? name} · <span className="font-mono">{name}</span>
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        </div>

        <label className="flex items-center gap-2 text-sm text-gray-300">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="text-accent"
          />
          Enabled
        </label>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button onClick={save} loading={busy} disabled={busy}>
            Save
          </Button>
          <Button
            variant="secondary"
            onClick={sendTest}
            disabled={busy || !config?.configured}
            title={config?.configured ? 'Post a test message' : 'Save a webhook first'}
          >
            Send test
          </Button>
          {config?.configured && (
            <Button variant="ghost" onClick={clear} disabled={busy}>
              Remove webhook
            </Button>
          )}
        </div>
      </Card>
    </div>
  );
}

// ── Blocklist ───────────────────────────────────────────────────────────────

function BlocklistPanel() {
  const [entries, setEntries] = useState<BlocklistEntry[]>([]);
  const [scope, setScope] = useState<'all' | BlocklistScope>('all');
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listBlocklist(scope === 'all' ? undefined : { scope });
      setEntries(data.entries);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    void load();
  }, [load]);

  async function unblock(entry: BlocklistEntry) {
    setBusyId(entry.id);
    setError(null);
    try {
      await api.removeBlock(entry.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <SectionHeader
        title="Blocklist"
        subtitle="Never offer or accept these again — enforced, not just hidden"
      />

      <div className="flex flex-wrap gap-2">
        {(['all', 'release_group', 'artist'] as const).map((value) => (
          <button
            key={value}
            onClick={() => setScope(value)}
            className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
              scope === value
                ? 'border-accent text-accent bg-accent/10'
                : 'border-surface-border text-gray-500 hover:text-gray-300'
            }`}
          >
            {value === 'all' ? 'Everything' : value === 'artist' ? 'Artists' : 'Albums'}
          </button>
        ))}
      </div>

      {error && (
        <Card className="p-3 border border-red-500/30">
          <p className="text-xs text-red-400">{error}</p>
        </Card>
      )}

      {loading && <Spinner text="Loading blocklist…" />}

      {!loading && entries.length === 0 && (
        <EmptyState
          icon="🚫"
          message="Nothing is blocked. Block an album from its detail view, or an artist from their page."
        />
      )}

      <div className="space-y-2">
        {entries.map((entry) => (
          <Card key={entry.id} className="p-3">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="text-sm text-white truncate">
                    {entry.scope === 'artist'
                      ? entry.artist_name ?? 'Unknown artist'
                      : entry.album_title ?? 'Untitled album'}
                  </p>
                  <Badge variant={entry.scope === 'artist' ? 'warning' : 'default'}>
                    {entry.scope === 'artist' ? 'artist' : 'album'}
                  </Badge>
                </div>
                <p className="mt-0.5 text-[11px] text-gray-500">
                  {entry.scope === 'release_group' && entry.artist_name
                    ? `${entry.artist_name} · `
                    : ''}
                  <span className="font-mono">
                    {entry.release_group_mbid ?? entry.artist_mbid}
                  </span>
                  {entry.created_by && <> · blocked by {entry.created_by}</>}
                  {entry.created_at && <> · {formatRelativeTime(entry.created_at)}</>}
                </p>
                {entry.reason && (
                  <p className="mt-1 text-[11px] text-gray-400 italic">“{entry.reason}”</p>
                )}
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => unblock(entry)}
                disabled={busyId === entry.id}
                loading={busyId === entry.id}
              >
                Unblock
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
