/**
 * Artist discography + the "request everything" panel.
 *
 * The panel asks the server to *plan* before offering to create: the count it
 * shows comes from MusicBrainz, minus what the library already has and minus
 * what is already requested. That is the difference between "Request 17 albums?"
 * and a switch that quietly queues 40.
 *
 * Above 15 albums the confirm needs a second, explicit click — a discography
 * request is the most expensive action in the app and the easiest to trigger by
 * accident.
 */
import { useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { ArtistDetail, ArtistRequestPlan, ArtistScope, CatalogCard } from '../types.ts';
import { Badge, Button, Card, EmptyState, Select, Spinner } from '../components.tsx';
import { AlbumCard } from '../catalog.tsx';

const BIG_REQUEST = 15;

const SCOPES: { value: ArtistScope; label: string }[] = [
  { value: 'studio', label: 'Studio albums (no soundtracks or compilations)' },
  { value: 'studio_eps', label: 'Studio albums + EPs' },
  { value: 'everything', label: 'Everything (albums, EPs, singles)' },
];

interface Props {
  artistMbid: string;
  artistName?: string | null;
  onOpenAlbum: (card: CatalogCard) => void;
  onBack: () => void;
  /** Called after a request is created so the requests list can refresh. */
  onRequested: () => void;
}

export function ArtistView({ artistMbid, artistName, onOpenAlbum, onBack, onRequested }: Props) {
  const [artist, setArtist] = useState<ArtistDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [scope, setScope] = useState<ArtistScope>('studio');
  const [plan, setPlan] = useState<ArtistRequestPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [confirmArmed, setConfirmArmed] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createdMessage, setCreatedMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPlan(null);
    setCreatedMessage(null);
    setConfirmArmed(false);

    api
      .getArtist(artistMbid)
      .then((detail) => {
        if (!cancelled) setArtist(detail);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [artistMbid]);

  // Re-plan whenever the scope changes: the count depends on it, and a stale
  // count in a confirm dialog is exactly the number the user is trusting.
  useEffect(() => {
    let cancelled = false;
    setPlanning(true);
    setPlanError(null);
    setConfirmArmed(false);
    setCreatedMessage(null);

    api
      .planArtistRequest(artistMbid, scope)
      .then((result) => {
        if (!cancelled) setPlan(result);
      })
      .catch((e: unknown) => {
        if (!cancelled) setPlanError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setPlanning(false);
      });

    return () => {
      cancelled = true;
    };
  }, [artistMbid, scope]);

  async function createRequest() {
    if (!plan) return;
    const count = plan.summary.requestable ?? 0;

    // Two-step confirm for large discographies.
    if (count > BIG_REQUEST && !confirmArmed) {
      setConfirmArmed(true);
      return;
    }

    setCreating(true);
    setPlanError(null);
    try {
      await api.createRequest({ kind: 'artist', artist_mbid: artistMbid, scope });
      setCreatedMessage(`Requested ${count} ${count === 1 ? 'album' : 'albums'}.`);
      setConfirmArmed(false);
      onRequested();
      const refreshed = await api.planArtistRequest(artistMbid, scope);
      setPlan(refreshed);
    } catch (e) {
      setPlanError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }

  if (loading) return <Spinner text="Loading discography from MusicBrainz…" />;

  if (error) {
    return (
      <div className="space-y-3">
        <Button size="sm" variant="ghost" onClick={onBack}>
          ← Back
        </Button>
        <Card className="p-4 border border-amber-500/30">
          <p className="text-sm text-amber-300">{error}</p>
        </Card>
      </div>
    );
  }

  if (!artist) return <EmptyState icon="🎤" message="No such artist." />;

  const summary = artist.summary;
  const requestable = plan?.summary.requestable ?? 0;
  const ownedInPlan = plan?.summary.owned ?? 0;
  const alreadyRequested = plan?.summary.already_requested ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" onClick={onBack}>
          ← Back
        </Button>
      </div>

      <div>
        <h2 className="text-lg font-semibold text-white">
          {artist.artist_name ?? artistName ?? 'Unknown artist'}
        </h2>
        {artist.disambiguation && (
          <p className="text-xs text-gray-500">{artist.disambiguation}</p>
        )}
        <p className="mt-1 text-xs text-gray-500">
          {summary.albums} albums &amp; EPs · {summary.owned} in your library
        </p>
      </div>

      {/* Discography request */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <p className="text-sm font-medium text-white">Request the discography</p>
            <p className="text-[11px] text-gray-500">
              Albums already in your library and already-requested ones are skipped.
            </p>
          </div>
          <Badge variant="info">{requestable} to request</Badge>
        </div>

        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <Select
            value={scope}
            onChange={(value) => setScope(value as ArtistScope)}
            options={SCOPES}
            label="What counts as the discography"
          />
          <Button
            onClick={createRequest}
            loading={creating}
            disabled={creating || planning || requestable === 0}
            variant={confirmArmed ? 'danger' : 'primary'}
          >
            {planning
              ? 'Planning…'
              : requestable === 0
                ? 'Nothing left to request'
                : confirmArmed
                  ? `Confirm: request ${requestable} albums`
                  : `Request ${requestable} ${requestable === 1 ? 'album' : 'albums'}`}
          </Button>
        </div>

        {plan && (
          <p className="text-[11px] text-gray-500">
            {ownedInPlan > 0 && <>{ownedInPlan} already in your library. </>}
            {alreadyRequested > 0 && <>{alreadyRequested} already requested. </>}
            {scope === 'studio' && 'Soundtracks and compilations are excluded — request those individually.'}
          </p>
        )}

        {confirmArmed && (
          <p className="text-[11px] text-amber-300">
            That is a lot of albums. Click the red button to confirm, or change the scope above.
          </p>
        )}

        {planError && <p className="text-xs text-red-400">{planError}</p>}
        {createdMessage && <p className="text-xs text-emerald-400">{createdMessage}</p>}
      </Card>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {artist.albums.map((card) => (
          <AlbumCard
            key={card.release_group_mbid}
            card={card}
            onOpen={onOpenAlbum}
          />
        ))}
      </div>

      {artist.albums.length === 0 && (
        <EmptyState icon="🎤" message="MusicBrainz lists no albums or EPs for this artist." />
      )}
    </div>
  );
}
