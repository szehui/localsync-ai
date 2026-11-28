/**
 * Home: two rails — new releases, and the most-listened among them.
 *
 * The rails degrade to hidden on failure rather than showing an error: this is
 * the landing view, and a MusicBrainz hiccup should not greet someone with a red
 * box before they have done anything. Search still works if they want it.
 */
import { useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { CatalogCard, RailsResponse } from '../types.ts';
import { Card, EmptyState, Rail, SectionHeader, Spinner } from '../components.tsx';
import { AlbumCard } from '../catalog.tsx';

interface Props {
  onOpenAlbum: (card: CatalogCard) => void;
  onOpenArtist: (artistMbid: string, artistName?: string | null) => void;
  onGoToSearch: () => void;
}

export function CatalogHome({ onOpenAlbum, onOpenArtist, onGoToSearch }: Props) {
  const [rails, setRails] = useState<RailsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getRails()
      .then((data) => {
        if (!cancelled) setRails(data);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <Spinner text="Loading new releases…" />;

  // Labels state the actual window and metric. "New this week" over a fortnight
  // of releases, and "most played right now" over all-time listen counts, would
  // both be claims the data does not support.
  const sections = [
    { key: 'new', title: 'New releases · last 14 days', cards: rails?.new_releases ?? [] },
    { key: 'trending', title: 'Most played among new releases', cards: rails?.trending ?? [] },
  ].filter((section) => section.cards.length > 0);

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Home"
        subtitle="Fresh releases worth a listen — request anything you do not have"
      />

      {failed && (
        <Card className="p-4">
          <p className="text-sm text-gray-400">
            Could not load new releases right now.{' '}
            <button
              onClick={onGoToSearch}
              className="text-accent hover:text-accent-muted underline"
            >
              Search instead
            </button>
            .
          </p>
        </Card>
      )}

      {!failed && sections.length === 0 && (
        <EmptyState
          icon="🎧"
          message="No new releases cached yet. Search for an album to get started."
        />
      )}

      {sections.map((section) => (
        <div key={section.key} className="space-y-2">
          <h3 className="text-sm font-medium text-gray-300">{section.title}</h3>
          <Rail label={section.title}>
            {section.cards.map((card) => (
              <AlbumCard
                key={`${section.key}-${card.release_group_mbid}`}
                card={card}
                onOpen={onOpenAlbum}
                onOpenArtist={(c) => c.artist_mbid && onOpenArtist(c.artist_mbid, c.artist_name)}
                rail
              />
            ))}
          </Rail>
        </div>
      ))}
    </div>
  );
}
