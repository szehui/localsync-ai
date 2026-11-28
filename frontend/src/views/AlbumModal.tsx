/**
 * Album detail + the request action.
 *
 * The button's label after a successful request comes from the server's
 * response, never from an optimistic guess: "Pending approval" and "Approved"
 * mean different things to the person waiting, and showing the wrong one is how
 * an app teaches people not to trust it.
 */
import { useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { AlbumDetail, RequestItem } from '../types.ts';
import { Badge, Button, Spinner } from '../components.tsx';
import { Cover, STATUS_STYLES, StatusPill } from '../catalog.tsx';

interface Props {
  releaseGroupMbid: string;
  onClose: () => void;
  onOpenArtist: (artistMbid: string, artistName?: string | null) => void;
  /** Fired after a request is created, so lists can refresh. */
  onRequested?: (request: RequestItem) => void;
  /** Nested only when the modal is opened from within another modal. */
  zIndex?: number;
}

export function AlbumModal({
  releaseGroupMbid,
  onClose,
  onOpenArtist,
  onRequested,
  zIndex = 50,
}: Props) {
  const [album, setAlbum] = useState<AlbumDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [note, setNote] = useState('');
  const [showNote, setShowNote] = useState(false);
  const [created, setCreated] = useState<RequestItem | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setAlbum(null);
    setError(null);
    setCreated(null);
    setRequestError(null);
    setNote('');
    setShowNote(false);

    api
      .getAlbum(releaseGroupMbid)
      .then((detail) => {
        if (!cancelled) setAlbum(detail);
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
  }, [releaseGroupMbid]);

  // Escape closes the top-most modal only.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  async function handleRequest() {
    setRequesting(true);
    setRequestError(null);
    try {
      const request = await api.createRequest({
        kind: 'album',
        release_group_mbid: releaseGroupMbid,
        note: note.trim() || undefined,
      });
      setCreated(request);
      onRequested?.(request);
      setShowNote(false);
    } catch (e) {
      setRequestError(e instanceof Error ? e.message : String(e));
    } finally {
      setRequesting(false);
    }
  }

  const status = created?.status ?? album?.request?.status ?? null;
  const alreadyRequested = !created && Boolean(album?.request);

  return (
    <div
      className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
      style={{ zIndex }}
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-surface-border bg-surface-raised p-5 shadow-2xl max-h-[90vh] overflow-y-auto animate-slide-up"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={album?.title ? `Album: ${album.title}` : 'Album details'}
      >
        <div className="flex items-start justify-between gap-3 mb-4">
          <h3 className="text-base font-semibold text-white">Album</h3>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-200 text-sm"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {loading && <Spinner text="Loading from MusicBrainz…" />}

        {error && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="text-sm text-amber-300">{error}</p>
            <p className="mt-1 text-[11px] text-gray-500">
              This is an upstream problem, not "no such album" — try again in a moment.
            </p>
          </div>
        )}

        {album && (
          <div className="space-y-4">
            <div className="flex gap-4">
              <Cover url={album.cover_url} title={album.title} size={112} />
              <div className="min-w-0">
                <p className="text-sm font-semibold text-white">{album.title ?? 'Untitled'}</p>
                {album.artist_mbid ? (
                  <button
                    onClick={() => onOpenArtist(album.artist_mbid!, album.artist_name)}
                    className="mt-0.5 text-sm text-accent hover:text-accent-muted transition-colors text-left"
                    title="See the full discography"
                  >
                    {album.artist_name ?? 'Unknown artist'}
                  </button>
                ) : (
                  <p className="mt-0.5 text-sm text-gray-400">{album.artist_name ?? 'Unknown artist'}</p>
                )}
                <p className="mt-1 text-xs text-gray-500">
                  {[
                    album.year,
                    album.primary_type,
                    album.secondary_types.length ? album.secondary_types.join(', ') : null,
                    album.releases.length
                      ? `${album.releases.length} ${album.releases.length === 1 ? 'edition' : 'editions'}`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {album.owned && (
                    <span title={album.owned_via === 'name' ? 'Name match, no MusicBrainz tag' : undefined}>
                      <Badge variant="success">
                        {album.owned_via === 'name' ? 'Probably in library' : 'In library'}
                      </Badge>
                    </span>
                  )}
                  {!album.requestable && <Badge variant="warning">Not an album or EP</Badge>}
                  {status && <StatusPill status={status} />}
                </div>
              </div>
            </div>

            {album.aliases.length > 0 && (
              <p className="text-[11px] text-gray-500">
                <span className="text-gray-600">Also known as:</span>{' '}
                {album.aliases.slice(0, 4).join(' · ')}
              </p>
            )}

            {album.releases.length > 0 && (
              <p className="text-[11px] text-gray-600">
                {album.releases.length} edition{album.releases.length === 1 ? '' : 's'} on
                MusicBrainz; Lidarr matches on the release group, so any edition satisfies this
                request.
              </p>
            )}

            {requestError && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3">
                <p className="text-sm text-red-400">{requestError}</p>
              </div>
            )}

            {created && (
              <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
                <p className="text-sm text-emerald-300">
                  {created.status === 'pending'
                    ? 'Requested — waiting for owner approval.'
                    : STATUS_STYLES[created.status]?.hint ?? 'Request created.'}
                </p>
                {created.vote_count > 1 && (
                  <p className="mt-1 text-[11px] text-gray-500">
                    {created.vote_count} people want this.
                  </p>
                )}
              </div>
            )}

            {!created && (
              <div className="space-y-2">
                {album.owned ? (
                  <p className="text-xs text-gray-500">
                    This is already in your library — nothing to request.
                  </p>
                ) : alreadyRequested ? (
                  <p className="text-xs text-gray-500">
                    Someone already asked for this; the request is still open.
                  </p>
                ) : !album.requestable ? (
                  <p className="text-xs text-gray-500">
                    MusicBrainz does not classify this as an album or EP, so it is not requestable
                    here.
                  </p>
                ) : (
                  <>
                    {showNote ? (
                      <textarea
                        value={note}
                        onChange={(event) => setNote(event.target.value)}
                        placeholder="Why this one? (optional — the owner sees it)"
                        rows={2}
                        maxLength={500}
                        className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent"
                      />
                    ) : (
                      <button
                        onClick={() => setShowNote(true)}
                        className="text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
                      >
                        + Add a note for the owner
                      </button>
                    )}
                    <Button onClick={handleRequest} loading={requesting} disabled={requesting}>
                      {requesting ? 'Requesting…' : 'Request album'}
                    </Button>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
