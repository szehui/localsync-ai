import { useEffect, useRef, useState } from 'react';
import { api } from '../api.ts';
import type { DiscoveryArtistGroup, DiscoveryFeed } from '../types.ts';
import { Badge, Button, Card, EmptyState, SectionHeader, Spinner } from '../components.tsx';

type Filter = 'all' | 'new_artist' | 'artist_known';

const POLL_MS = 3000;

export function DiscoveryFeedView() {
  const [feed, setFeed] = useState<DiscoveryFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState<Filter>('all');
  const [error, setError] = useState<string | null>(null);
  const [busyOffer, setBusyOffer] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  async function load() {
    try {
      const data = await api.getDiscoveryFeed();
      setFeed(data);
      setError(null);
      return data;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, []);

  // Poll while the backend reports a refresh in flight.
  function startPolling() {
    if (pollRef.current !== null) return;
    pollRef.current = window.setInterval(async () => {
      const data = await load();
      if (data && data.state.last_status !== 'running') {
        setRefreshing(false);
        if (pollRef.current !== null) {
          window.clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }
    }, POLL_MS);
  }

  async function handleRefresh() {
    setRefreshing(true);
    setError(null);
    try {
      await api.refreshDiscovery();
      await load();
      startPolling();
    } catch (e) {
      setRefreshing(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleAdd(group: DiscoveryArtistGroup) {
    setBusyOffer(group.representative_offer_id);
    try {
      const result = await api.addDiscoveryOfferToLidarr(group.representative_offer_id);
      setToast(
        `${result.message}: ${result.artist_name ?? ''} — ${result.release_name ?? ''}`,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyOffer(null);
    }
  }

  async function handleDismiss(group: DiscoveryArtistGroup) {
    setBusyOffer(group.representative_offer_id);
    try {
      await api.dismissDiscoveryOffer(group.representative_offer_id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyOffer(null);
    }
  }

  if (loading) return <Spinner text="Loading discovery feed..." />;

  const state = feed?.state;
  const counts = feed?.counts;
  const isRunning = refreshing || state?.last_status === 'running';

  const artists = (feed?.artists ?? []).filter(
    (g) => filter === 'all' || g.bucket === filter,
  );

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Discover"
        subtitle="Music worth buying that isn't in your library yet"
        action={
          <Button onClick={handleRefresh} loading={isRunning} disabled={isRunning}>
            {isRunning ? 'Refreshing…' : 'Refresh feed'}
          </Button>
        }
      />

      {/* Run summary */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-gray-400">
          <span>
            <span className="text-gray-500">New artists:</span>{' '}
            <span className="text-white font-semibold">{counts?.new_artist ?? 0}</span>
          </span>
          <span>
            <span className="text-gray-500">Gap fills:</span>{' '}
            <span className="text-white font-semibold">{counts?.artist_known ?? 0}</span>
          </span>
          <span>
            <span className="text-gray-500">Candidates:</span>{' '}
            <span className="text-white font-semibold">{counts?.total_offers ?? 0}</span>
          </span>
          <span>
            <span className="text-gray-500">Seeds:</span>{' '}
            <span className="text-white font-semibold">{state?.seeds_used ?? 0}</span>
          </span>
          <span>
            <span className="text-gray-500">API calls:</span>{' '}
            <span className="text-white font-semibold">{state?.calls_made ?? 0}</span>
          </span>
          {state?.last_status && (
            <Badge
              variant={
                state.last_status === 'ok'
                  ? 'success'
                  : state.last_status === 'running'
                    ? 'warning'
                    : 'danger'
              }
              dot
            >
              {state.last_status}
            </Badge>
          )}
        </div>
        {state?.last_error && (
          <p className="mt-2 text-xs text-red-400">{state.last_error}</p>
        )}
        {state?.last_run && (
          <p className="mt-2 text-[11px] text-gray-600">
            Last refresh {new Date(state.last_run + 'Z').toLocaleString()}
          </p>
        )}
      </Card>

      {error && (
        <Card className="p-4 border border-red-500/30">
          <p className="text-sm text-red-400">{error}</p>
        </Card>
      )}

      {toast && (
        <Card className="p-4 border border-emerald-500/30">
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-emerald-400">{toast}</p>
            <button
              className="text-xs text-gray-500 hover:text-gray-300"
              onClick={() => setToast(null)}
            >
              dismiss
            </button>
          </div>
        </Card>
      )}

      {/* Filters */}
      <div className="flex gap-2">
        {(
          [
            ['all', 'All'],
            ['new_artist', 'New artists'],
            ['artist_known', 'Gap fills'],
          ] as [Filter, string][]
        ).map(([value, label]) => (
          <button
            key={value}
            onClick={() => setFilter(value)}
            className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
              filter === value
                ? 'border-accent text-accent bg-accent/10'
                : 'border-surface-border text-gray-500 hover:text-gray-300'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {isRunning && (
        <Spinner text="Expanding seeds against ListenBrainz — up to a minute on a cold cache" />
      )}

      {!isRunning && artists.length === 0 && (
        <EmptyState
          icon="🔭"
          message={
            state?.last_run
              ? 'No new candidates right now. Everything found has been added or dismissed — try refreshing later.'
              : 'No discovery run yet. Hit "Refresh feed" to expand your top-played tracks into new artists.'
          }
        />
      )}

      <div className="space-y-3">
        {artists.map((group) => {
          const busy = busyOffer === group.representative_offer_id;
          const seeds = group.evidence.slice(0, 2);
          return (
            <Card key={`${group.artist_name}-${group.representative_offer_id}`} className="p-4" hover>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h3 className="text-sm font-semibold text-white truncate">
                      {group.artist_name ?? 'Unknown artist'}
                    </h3>
                    <Badge variant={group.bucket === 'new_artist' ? 'success' : 'default'}>
                      {group.bucket === 'new_artist' ? 'new to you' : 'in library'}
                    </Badge>
                    {group.offers.length > 1 && (
                      <span className="text-[11px] text-gray-500">
                        {group.offers.length} tracks
                      </span>
                    )}
                  </div>

                  <p className="mt-1 text-sm text-gray-300">
                    {group.representative_release ? (
                      <>
                        <span className="text-gray-500">Album:</span>{' '}
                        {group.representative_release}
                        {group.representative_title && (
                          <span className="text-gray-600"> · {group.representative_title}</span>
                        )}
                      </>
                    ) : (
                      group.representative_title ?? '—'
                    )}
                  </p>

                  {seeds.length > 0 && (
                    <p className="mt-1 text-[11px] text-gray-500">
                      Because you played{' '}
                      {seeds.map((s, i) => (
                        <span key={`${s.seed_artist}-${s.seed_title}-${i}`}>
                          {i > 0 && ', '}
                          <span className="text-gray-400">
                            {s.seed_artist} — {s.seed_title}
                          </span>
                        </span>
                      ))}
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  <span
                    title={
                      group.representative_release
                        ? 'Add just this album to Lidarr'
                        : 'No album name to look up'
                    }
                  >
                    <Button
                      size="sm"
                      onClick={() => handleAdd(group)}
                      disabled={busy || !group.representative_release}
                      loading={busy}
                    >
                      Add album
                    </Button>
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => handleDismiss(group)}
                    disabled={busy}
                  >
                    Dismiss
                  </Button>
                </div>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
