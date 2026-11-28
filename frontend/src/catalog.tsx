/**
 * Shared catalog pieces: the album card, the status pill, and the request
 * action.
 *
 * Two rules this file exists to enforce, because both were violated in earlier
 * iterations of the plan:
 *
 * 1. **A status pill renders the server's string.** No client-side inference —
 *    no "it's been a while, must be downloading". If the label is wrong, the
 *    reconciler is wrong, and that is a backend bug worth seeing.
 * 2. **A card shows exactly one of three things**: owned, requested (by
 *    someone), or requestable. A bare "Request" button next to an album that is
 *    already owned invites a duplicate Lidarr add.
 */
import type { CatalogCard, RequestStatus } from './types';
import { Badge, Button } from './components';

const cx = (...classes: (string | false | null | undefined)[]) =>
  classes.filter(Boolean).join(' ');

// ─── Status pill ─────────────────────────────────────────────────────────────

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info';

interface StatusStyle {
  variant: BadgeVariant;
  label: string;
  /** Long form for tooltips: why this status, in the user's terms. */
  hint: string;
}

/**
 * One entry per status in the server's state machine. Exhaustive by type: adding
 * a status server-side fails the build here until the UI says what it means,
 * which is the only way to stop a new status rendering as "unknown".
 */
export const STATUS_STYLES: Record<RequestStatus, StatusStyle> = {
  pending: {
    variant: 'warning',
    label: 'Pending approval',
    hint: 'Waiting for the owner to approve',
  },
  approved: {
    variant: 'info',
    label: 'Approved',
    hint: 'Sent to Lidarr, waiting for a download',
  },
  downloading: {
    variant: 'info',
    label: 'Downloading',
    hint: 'A download client is fetching this now',
  },
  partially_available: {
    variant: 'warning',
    label: 'Partly here',
    hint: 'Some tracks have imported, not all',
  },
  imported: {
    variant: 'info',
    label: 'Imported',
    hint: 'Lidarr has the files; Navidrome has not scanned them yet',
  },
  available: {
    variant: 'success',
    label: 'In library',
    hint: 'Navidrome can play this now',
  },
  failed: {
    variant: 'danger',
    label: 'Failed',
    hint: 'Lidarr could not deliver this — see the message',
  },
  denied: { variant: 'default', label: 'Denied', hint: 'Declined by the owner' },
  cancelled: { variant: 'default', label: 'Cancelled', hint: 'Withdrawn' },
};

export function StatusPill({ status }: { status: RequestStatus }) {
  const style = STATUS_STYLES[status] ?? {
    variant: 'default' as BadgeVariant,
    label: status,
    hint: '',
  };
  return (
    <span title={style.hint}>
      <Badge variant={style.variant} dot>
        {style.label}
      </Badge>
    </span>
  );
}

// ─── Cover art ───────────────────────────────────────────────────────────────

export function Cover({
  url,
  title,
  size = 64,
}: {
  url?: string | null;
  title?: string | null;
  size?: number;
}) {
  return (
    <div
      className="rounded-lg overflow-hidden bg-surface-overlay border border-surface-border shrink-0 flex items-center justify-center"
      style={{ width: size, height: size }}
    >
      {url ? (
        <img
          src={url}
          alt={title ? `Cover of ${title}` : ''}
          loading="lazy"
          className="w-full h-full object-cover"
        />
      ) : (
        <svg
          className="w-1/3 h-1/3 text-gray-600"
          viewBox="0 0 24 24"
          fill="currentColor"
          aria-hidden="true"
        >
          <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z" />
        </svg>
      )}
    </div>
  );
}

// ─── The card ────────────────────────────────────────────────────────────────

export interface AlbumCardProps {
  card: CatalogCard;
  /** Opens the album detail (which owns the Request action). */
  onOpen: (card: CatalogCard) => void;
  onOpenArtist?: (card: CatalogCard) => void;
  /** Shown in a horizontal rail: fixed width, no description line. */
  rail?: boolean;
  busy?: boolean;
}

export function AlbumCard({ card, onOpen, onOpenArtist, rail = false, busy = false }: AlbumCardProps) {
  const owned = card.owned;
  const requested = card.request;

  // "Probably owned": a folded artist|title match with no MBID. Labelled
  // differently on purpose — 716 albums here are single-track rips that no MBID
  // backfill will ever reach, and calling those "not owned" invites a duplicate
  // purchase.
  const badge = owned ? (
    <span title={
      card.owned_via === 'name'
        ? 'Matched on artist and album name — this library copy has no MusicBrainz tag'
        : 'Matched by MusicBrainz release-group id'
    }>
      <Badge variant="success">
        {card.owned_via === 'name' ? 'Probably in library' : 'In library'}
      </Badge>
    </span>
  ) : requested ? (
    <span title={requested.requested_by ? `Requested by ${requested.requested_by}` : undefined}>
      <Badge variant={requested.status === 'pending' ? 'warning' : 'info'}>
        {requested.status === 'pending'
          ? 'Requested'
          : STATUS_STYLES[requested.status]?.label ?? requested.status}
      </Badge>
    </span>
  ) : null;

  return (
    <div
      className={cx(
        'group text-left rounded-xl border border-surface-border bg-surface-raised p-3 transition-all duration-200',
        'hover:border-surface-highlight hover:shadow-lg hover:shadow-black/20',
        rail && 'w-40 shrink-0',
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(card)}
        disabled={busy}
        className="w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded-lg"
        aria-label={`Open ${card.title ?? 'album'}`}
      >
        <div className="relative">
          <Cover url={card.cover_url} title={card.title} size={rail ? 136 : 64} />
        </div>
        <div className={cx('mt-2 min-w-0', !rail && 'flex-1')}>
          <p className="text-sm font-medium text-white truncate" title={card.title ?? ''}>
            {card.title ?? 'Untitled'}
          </p>
          {card.artist_name && (
            <p className="text-xs text-gray-500 truncate">
              {card.artist_name}
              {card.year ? <span className="text-gray-600"> · {card.year}</span> : null}
            </p>
          )}
          {badge && <div className="mt-1.5">{badge}</div>}
        </div>
      </button>
      {/* Rail cards keep this hidden until hover: repeated under 24 cards in a
          horizontal strip it is a wall of grey text, not an affordance. */}
      {card.artist_mbid && onOpenArtist && (
        <button
          type="button"
          onClick={() => onOpenArtist(card)}
          className={cx(
            'mt-1.5 text-[11px] text-gray-600 hover:text-accent transition-colors truncate w-full text-left',
            rail && 'opacity-0 group-hover:opacity-100 focus:opacity-100',
          )}
          title="See this artist's discography"
        >
          More by {card.artist_name ?? 'this artist'} →
        </button>
      )}
    </div>
  );
}

/** Row layout (search results), same information, wider. */
export function AlbumRow({ card, onOpen, onOpenArtist }: AlbumCardProps) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-surface-border bg-surface-raised p-3 hover:border-surface-highlight transition-colors">
      <button
        type="button"
        onClick={() => onOpen(card)}
        className="flex items-center gap-3 flex-1 min-w-0 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded-lg"
      >
        <Cover url={card.cover_url} title={card.title} size={48} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-white truncate">{card.title ?? 'Untitled'}</p>
          <p className="text-xs text-gray-500 truncate">
            {[
              card.artist_name,
              card.year,
              card.primary_type,
              card.secondary_types.length ? card.secondary_types.join(', ') : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
        </div>
      </button>

      <div className="flex items-center gap-2 shrink-0">
        {card.owned ? (
          <span title={card.owned_via === 'name' ? 'Name match, no MusicBrainz tag on the library copy' : undefined}>
            <Badge variant="success">
              {card.owned_via === 'name' ? 'Probably in library' : 'In library'}
            </Badge>
          </span>
        ) : card.request ? (
          <StatusPill status={card.request.status} />
        ) : (
          <Button size="sm" variant="accent" onClick={() => onOpen(card)}>
            Request
          </Button>
        )}
        {card.artist_mbid && onOpenArtist && (
          <Button size="sm" variant="ghost" onClick={() => onOpenArtist(card)} title="Discography">
            Artist
          </Button>
        )}
      </div>
    </div>
  );
}
