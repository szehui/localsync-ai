/**
 * The requests queue: what was asked for, and what actually happened.
 *
 * Two modes by permission — Mine (default) and All (owner). Every action here
 * is owner-gated server-side; this view only decides what to *offer*, and a 403
 * from a stale tab is shown as a plain message rather than hidden.
 *
 * Statuses are rendered from the server's strings. The one piece of derived
 * information is for artist requests, whose children are the detail: the parent
 * row shows the rollup the server computed, never a count this view tallied.
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { RequestItem, RequestStatus } from '../types.ts';
import {
  Badge,
  Button,
  Card,
  EmptyState,
  SectionHeader,
  Spinner,
  formatRelativeTime,
} from '../components.tsx';
import { Cover, StatusPill } from '../catalog.tsx';

type Scope = 'mine' | 'all';

const FILTERS: { value: 'open' | 'all' | RequestStatus; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'all', label: 'Everything' },
  { value: 'available', label: 'Available' },
  { value: 'failed', label: 'Failed' },
  { value: 'pending', label: 'Awaiting approval' },
];

const OPEN_STATUSES: RequestStatus[] = [
  'pending',
  'approved',
  'downloading',
  'partially_available',
  'imported',
];

interface Props {
  isOwner: boolean;
  /** Bumped by the parent when something elsewhere creates a request. */
  refreshKey?: number;
}

export function RequestsView({ isOwner, refreshKey = 0 }: Props) {
  const [scope, setScope] = useState<Scope>('mine');
  const [filter, setFilter] = useState<'open' | 'all' | RequestStatus>('open');
  const [requests, setRequests] = useState<RequestItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [denyFor, setDenyFor] = useState<number | null>(null);
  const [denyReason, setDenyReason] = useState('');
  const [syncing, setSyncing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listRequests({ scope });
      setRequests(data.requests);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function act(id: number, action: () => Promise<unknown>, message: string) {
    setBusyId(id);
    setNotice(null);
    setError(null);
    try {
      await action();
      setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  }

  async function handleSync() {
    setSyncing(true);
    setNotice(null);
    setError(null);
    try {
      const result = await api.syncRequests();
      setNotice(
        `Checked ${result.total} request${result.total === 1 ? '' : 's'}: ${result.changed} changed` +
          (result.failed ? `, ${result.failed} failed` : '') +
          '.',
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }

  const visible = requests.filter((request) => {
    if (request.kind === 'artist') {
      // Artist parents always show: their children are the detail.
      return true;
    }
    if (filter === 'all') return true;
    if (filter === 'open') return OPEN_STATUSES.includes(request.status);
    return request.status === filter;
  });

  return (
    <div className="space-y-5">
      <SectionHeader
        title="Requests"
        subtitle="What the household has asked for, and where each one actually is"
        action={
          isOwner ? (
            <Button onClick={handleSync} loading={syncing} disabled={syncing} variant="secondary">
              {syncing ? 'Checking…' : 'Check now'}
            </Button>
          ) : undefined
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        {isOwner && (
          <div className="flex rounded-lg border border-surface-border overflow-hidden">
            {(['mine', 'all'] as Scope[]).map((value) => (
              <button
                key={value}
                onClick={() => setScope(value)}
                className={`px-3 py-1.5 text-xs transition-colors ${
                  scope === value
                    ? 'bg-accent/15 text-accent'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {value === 'mine' ? 'Mine' : 'Everyone'}
              </button>
            ))}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {FILTERS.map((option) => (
            <button
              key={option.value}
              onClick={() => setFilter(option.value)}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
                filter === option.value
                  ? 'border-accent text-accent bg-accent/10'
                  : 'border-surface-border text-gray-500 hover:text-gray-300'
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

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

      {loading && <Spinner text="Loading requests…" />}

      {!loading && visible.length === 0 && (
        <EmptyState
          icon="📥"
          message={
            filter === 'open'
              ? 'Nothing in flight. Find an album in Search and request it.'
              : 'No requests match this filter.'
          }
        />
      )}

      <div className="space-y-2">
        {visible.map((request) => {
          const busy = busyId === request.id;
          const isArtist = request.kind === 'artist';
          // The server allows cancel only while pending (cancelling after
          // approval would leave the album in Lidarr's wanted list untracked),
          // so the button appears in exactly that window.
          const canCancel = request.status === 'pending';
          const canVote =
            !isOwner &&
            request.requested_by !== undefined &&
            OPEN_STATUSES.includes(request.status);
          return (
            <Card key={request.id} className="p-3">
              <div className="flex items-start gap-3">
                <Cover
                  url={request.cover_url}
                  title={request.album_title ?? request.artist_name ?? undefined}
                  size={48}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-sm font-medium text-white truncate">
                      {isArtist
                        ? `${request.artist_name ?? 'Unknown artist'} — discography`
                        : request.album_title ?? 'Untitled'}
                    </p>
                    {isArtist && <Badge variant="default">{request.scope ?? 'studio'}</Badge>}
                    <StatusPill status={request.status} />
                    {request.vote_count > 1 && (
                      <span
                        className="text-[11px] text-gray-500"
                        title={`${request.vote_count} people asked for this`}
                      >
                        ▲ {request.vote_count}
                      </span>
                    )}
                  </div>

                  <p className="mt-0.5 text-[11px] text-gray-500">
                    {[request.artist_name, request.year].filter(Boolean).join(' · ')}
                    {request.requested_by && (
                      <>
                        {request.artist_name || request.year ? ' · ' : ''}
                        asked by <span className="text-gray-400">{request.requested_by}</span>
                      </>
                    )}
                    {request.requested_at && <> · {formatRelativeTime(request.requested_at)}</>}
                  </p>

                  {isArtist && Object.keys(request.rollup).length > 0 && (
                    <p className="mt-1 text-[11px] text-gray-500">
                      {summarizeRollup(request.rollup)}
                    </p>
                  )}

                  {request.note && (
                    <p className="mt-1 text-[11px] text-gray-400 italic">“{request.note}”</p>
                  )}

                  {request.last_error && (
                    <p className="mt-1 text-[11px] text-red-400">{request.last_error}</p>
                  )}
                </div>

                <div className="flex flex-col items-end gap-1.5 shrink-0">
                  {isOwner && request.status === 'pending' && (
                    <div className="flex gap-1.5">
                      <Button
                        size="sm"
                        onClick={() =>
                          act(request.id, () => api.approveRequest(request.id), 'Approved.')
                        }
                        disabled={busy}
                        loading={busy}
                      >
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setDenyFor(request.id);
                          setDenyReason('');
                        }}
                        disabled={busy}
                      >
                        Deny
                      </Button>
                    </div>
                  )}

                  {/* Retry means "this broke, try again". An approved request is
                      already moving on its own — the reconciler owns that, and
                      "Check now" is how you force it. */}
                  {isOwner && request.status === 'failed' && (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() =>
                        act(request.id, () => api.retryRequest(request.id), 'Re-sent to Lidarr.')
                      }
                      disabled={busy}
                      loading={busy}
                    >
                      Retry
                    </Button>
                  )}

                  {canCancel && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        act(request.id, () => api.cancelRequest(request.id), 'Cancelled.')
                      }
                      disabled={busy}
                    >
                      Cancel
                    </Button>
                  )}

                  {canVote && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        act(request.id, () => api.voteRequest(request.id), 'Added your vote.')
                      }
                      disabled={busy}
                      title="Add your vote — the owner sees how many people want this"
                    >
                      ▲ Want this
                    </Button>
                  )}
                </div>
              </div>

              {denyFor === request.id && (
                <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-surface-border pt-3">
                  <input
                    value={denyReason}
                    onChange={(event) => setDenyReason(event.target.value)}
                    placeholder="Reason (optional — the requester sees it)"
                    maxLength={300}
                    className="flex-1 min-w-[220px] rounded-lg border border-surface-border bg-surface-overlay px-3 py-1.5 text-xs text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30"
                  />
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => {
                      const reason = denyReason.trim() || undefined;
                      setDenyFor(null);
                      void act(
                        request.id,
                        () => api.denyRequest(request.id, reason),
                        'Denied.',
                      );
                    }}
                    disabled={busy}
                  >
                    Confirm deny
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setDenyFor(null)}>
                    Keep
                  </Button>
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}

/** Turn `{requested: 17, available: 3, downloading: 2}` into a sentence. */
function summarizeRollup(rollup: Record<string, number>): string {
  const parts = [
    ['available', 'in library'],
    ['imported', 'imported'],
    ['partially_available', 'partial'],
    ['downloading', 'downloading'],
    ['approved', 'approved'],
    ['pending', 'pending'],
    ['failed', 'failed'],
    ['denied', 'denied'],
  ] as const;
  const described = parts
    .filter(([key]) => (rollup[key] ?? 0) > 0)
    .map(([key, label]) => `${rollup[key]} ${label}`);
  const total = rollup.requested ?? 0;
  return `${total} album${total === 1 ? '' : 's'}: ${described.join(' · ') || 'none yet'}`;
}
