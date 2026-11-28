/**
 * Catalog search.
 *
 * Three details that matter more than they look:
 *
 * - **Debounce, then abort.** 400 ms of quiet, and the in-flight request is
 *   cancelled when the query changes. Without the abort, a slow response for
 *   "daft" can land after "daft punk" and replace correct results with stale
 *   ones — the classic search box that shows the wrong thing and looks broken.
 * - **A 503 is not "no results".** MusicBrainz throttles; saying "no matches"
 *   when the query never ran is a lie the user acts on.
 * - **Artist fallback.** Typing an artist name into an album box is the most
 *   common miss, so an empty result offers the artist instead of a dead end.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.ts';
import type { ArtistSuggestion, CatalogCard, CatalogSearchResponse } from '../types.ts';
import { Button, Card, EmptyState, Input, Spinner } from '../components.tsx';
import { AlbumRow } from '../catalog.tsx';

const DEBOUNCE_MS = 400;

type Phase = 'idle' | 'typing' | 'loading' | 'results' | 'empty' | 'error';

interface Props {
  onOpenAlbum: (card: CatalogCard) => void;
  onOpenArtist: (artistMbid: string, artistName?: string | null) => void;
}

export function CatalogSearch({ onOpenAlbum, onOpenArtist }: Props) {
  const [query, setQuery] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [data, setData] = useState<CatalogSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<number | null>(null);

  const run = useCallback(async (q: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setPhase('loading');
    setError(null);
    try {
      const response = await api.searchCatalog(q, { limit: 25, signal: controller.signal });
      if (controller.signal.aborted) return;
      setData(response);
      const hasResults = response.results.length > 0;
      const hasArtists = response.artist_suggestions.length > 0;
      setPhase(hasResults || hasArtists ? 'results' : 'empty');
    } catch (e) {
      // An aborted request is not an error state — the user is still typing.
      if (controller.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) {
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
      setPhase('error');
    }
  }, []);

  useEffect(() => {
    const trimmed = query.trim();

    if (timerRef.current !== null) window.clearTimeout(timerRef.current);

    if (trimmed.length < 2) {
      abortRef.current?.abort();
      setPhase(trimmed.length === 0 ? 'idle' : 'typing');
      setData(null);
      return;
    }

    setPhase('typing');
    timerRef.current = window.setTimeout(() => {
      void run(trimmed);
    }, DEBOUNCE_MS);

    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, [query, run]);

  // Cancel anything in flight when the view unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  function openArtist(suggestion: ArtistSuggestion) {
    onOpenArtist(suggestion.artist_mbid, suggestion.artist_name);
  }

  return (
    <div className="space-y-5">
      <Card className="p-4">
        <Input
          value={query}
          onChange={setQuery}
          placeholder="Search albums and artists — try “Daft Punk” or “Homogenic”"
          label="Search the catalog"
          className="text-base"
        />
        <p className="mt-2 text-[11px] text-gray-600">
          Album data comes from MusicBrainz, so anything released — not just what you own.
        </p>
      </Card>

      {phase === 'idle' && (
        <EmptyState
          icon="🔎"
          message="Search for an album to see whether you already have it, or to ask for it."
        />
      )}

      {phase === 'loading' && <Spinner text="Searching MusicBrainz…" />}

      {phase === 'error' && (
        <Card className="p-4 border border-amber-500/30">
          <p className="text-sm text-amber-300">{error ?? 'Search failed.'}</p>
          <div className="mt-3">
            <Button size="sm" variant="secondary" onClick={() => void run(query.trim())}>
              Try again
            </Button>
          </div>
        </Card>
      )}

      {/* The artist strip sits above the results, not only in place of them.
          Searching "twice" returns nine albums titled *Twice* by unrelated
          artists and buries TWICE entirely; without this row there is no route
          from that result page to the band the person actually meant. */}
      {phase === 'results' && data && data.artist_suggestions.length > 0 && (
        <Card className="p-3">
          <p className="text-xs text-gray-500 mb-2">
            {data.results.length > 0 ? 'Artists matching this search' : 'Did you mean an artist?'}
          </p>
          <div className="flex flex-wrap gap-2">
            {data.artist_suggestions.map((suggestion) => (
              <Button
                key={suggestion.artist_mbid}
                size="sm"
                variant="secondary"
                onClick={() => openArtist(suggestion)}
              >
                {suggestion.artist_name}
                {suggestion.disambiguation && (
                  <span className="text-gray-500">({suggestion.disambiguation})</span>
                )}
              </Button>
            ))}
          </div>
        </Card>
      )}

      {phase === 'results' && data && data.results.length > 0 && (
        <div className="space-y-2">
          {data.results.map((card) => (
            <AlbumRow
              key={card.release_group_mbid}
              card={card}
              onOpen={onOpenAlbum}
              onOpenArtist={(c) => c.artist_mbid && onOpenArtist(c.artist_mbid, c.artist_name)}
            />
          ))}
        </div>
      )}

      {phase === 'results' && data && data.results.length === 0 && (
        <p className="text-sm text-gray-500">
          No album matches for “{query.trim()}”.
        </p>
      )}

      {phase === 'empty' && (
        <EmptyState icon="🔭" message="Nothing matched. Try the artist name, or a shorter title." />
      )}

      {phase === 'typing' && <p className="text-xs text-gray-600">Keep typing…</p>}
    </div>
  );
}
