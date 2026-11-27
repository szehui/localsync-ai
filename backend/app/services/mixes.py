"""Library-resident mixes — playlists built only from music you already own.

These are the ideas worth taking from Aurral's mixes: instead of reaching for an
external catalog (which can only ever produce 30-second previews you cannot play
in Navidrome), every mix here is computed from the local `tracks` table plus the
play counts and last-played timestamps Navidrome already exposes.

Play state is per user, so it lives in `track_annotations` (one row per
user+track), never on the shared `Track` row: the same song can be a favourite
for one listener and unheard by another, and every selector below therefore
reads annotations for the user it was asked about.

All selectors are read-only and return plain dicts::

    {"id": <navidrome song id>, "artist": str, "genre": str,
     "score": float, "reason": str}

Callers push the ids to Navidrome; the extra fields exist so the scheduler's
diversity pass (and any future UI) can see why a track was picked.

`last_played_at` only fills in on a library sync — Navidrome returns a `played`
timestamp per song. Until then the time-window mixes (on_repeat / rewind /
rediscover) return nothing and say so in the log, which is the honest result:
without play history there is no "you haven't heard this in a year" to find.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.database import Track, TrackAnnotation
from app.services.curation import (
    familiarity_weight,
    interleave,
    seeded_rng,
    weighted_sample,
)

logger = logging.getLogger(__name__)

# Name -> defaults. `limit` is the target track count; per-mix knobs override
# the function defaults below, so a trigger row only needs to store `mix_kind`.
MIX_SPECS: dict[str, dict] = {
    "deep_cuts": {
        "label": "Deep Cuts",
        "description": "Lesser-known tracks by the artists you play most",
        "limit": 50,
        "top_artists": 10,
        "per_artist": 3,
        "skip_top": 5,
    },
    "on_repeat": {
        "label": "On Repeat",
        "description": "What you have actually been playing lately",
        "limit": 50,
        "min_plays": 5,
        "within_days": 30,
    },
    "rewind": {
        "label": "Rewind",
        "description": "Heavy rotation from earlier this year",
        "limit": 50,
        "min_plays": 5,
        "older_than_days": 30,
        "within_days": 365,
    },
    "rediscover": {
        "label": "Rediscover",
        "description": "Old favourites you have not touched in a year or more",
        "limit": 50,
        "min_plays": 3,
        "older_than_days": 365,
    },
    "unplayed": {
        "label": "Unplayed",
        "description": "Never-played tracks by artists already in your rotation",
        "limit": 50,
        "min_played_library": 50,
    },
}

DEFAULT_MIX = "deep_cuts"


def _plays():
    """This user's play count for a track, 0 when they have no annotation."""
    return func.coalesce(TrackAnnotation.play_count, 0)


def _join_annotations(query, user_id: int):
    """LEFT JOIN one user's annotation row onto a `Track` query."""
    return query.outerjoin(
        TrackAnnotation,
        and_(
            TrackAnnotation.track_id == Track.id,
            TrackAnnotation.user_id == user_id,
        ),
    )


def annotated_tracks(db: Session, user_id: int):
    """Track rows left-joined to one user's annotations.

    Rows come back as ``(Track, play_count, rating, starred, last_played_at)``.

    The join is a LEFT OUTER JOIN and every column is coalesced because a track
    the user has never played has no annotation row at all — and it still has to
    be selectable. That is precisely the population the Unplayed mix exists to
    find: "in my library, never played by me". An inner join here would silently
    shrink the library to only-what-you-have-already-played and make Unplayed
    permanently empty.
    """
    return (
        db.query(
            Track,
            func.coalesce(TrackAnnotation.play_count, 0).label("play_count"),
            func.coalesce(TrackAnnotation.rating, 0).label("rating"),
            func.coalesce(TrackAnnotation.starred, False).label("starred"),
            TrackAnnotation.last_played_at.label("last_played_at"),
        )
        .outerjoin(
            TrackAnnotation,
            and_(
                TrackAnnotation.track_id == Track.id,
                TrackAnnotation.user_id == user_id,
            ),
        )
    )


def _to_dict(track: Track, score: float, reason: str) -> dict:
    return {
        "id": track.id,
        "artist": (track.artist_name or "").strip(),
        "genre": (track.genre or "").strip(),
        "score": float(score),
        "reason": reason,
    }


def select_deep_cuts(
    db: Session,
    user_id: int,
    limit: int = 50,
    top_artists: int = 10,
    per_artist: int = 3,
    skip_top: int = 5,
    rng=None,
) -> list[dict]:
    """Lesser-known tracks by the artists you play most.

    Aurral's version takes an artist's Deezer top 50, skips the first five (the
    hits) and keeps three. Transposed onto the local library: rank each artist's
    owned tracks by this user's play count, skip that artist's top `skip_top`,
    then weighted-sample `per_artist` of what remains — biased toward the
    least-played, so the same three do not come back every week. Lists are
    interleaved so one artist never occupies a contiguous block.
    """
    rng = rng or seeded_rng("deep_cuts")
    plays = _plays()
    artist_rows = (
        _join_annotations(
            db.query(Track.artist_name, func.sum(plays).label("plays")), user_id
        )
        .filter(plays > 0, Track.artist_name.isnot(None))
        .group_by(Track.artist_name)
        .order_by(func.sum(plays).desc())
        .limit(max(0, int(top_artists)))
        .all()
    )

    per_artist_lists: list[list[dict]] = []
    for artist_name, _plays_total in artist_rows:
        candidates = (
            annotated_tracks(db, user_id)
            .filter(Track.artist_name == artist_name, plays > 0)
            .order_by(plays.desc())
            .offset(max(0, int(skip_top)))
            .limit(200)
            .all()
        )
        if not candidates:
            continue
        # (track, this user's play count) — the weight has to come from the
        # annotation, not the dead Track column.
        pool = [(row[0], int(row[1] or 0)) for row in candidates]
        picked = weighted_sample(
            pool,
            lambda pair: familiarity_weight(pair[1], "deep"),
            per_artist,
            rng,
        )
        per_artist_lists.append(
            [_to_dict(track, familiarity_weight(track_plays, "deep"),
                      f"deep_cut:{artist_name}")
             for track, track_plays in picked]
        )

    if not per_artist_lists:
        logger.info("Deep Cuts: no artist had played tracks beyond their top %d", skip_top)
        return []

    ordered = interleave(per_artist_lists)
    logger.info(
        "Deep Cuts: %d artist(s) -> %d track(s)", len(per_artist_lists), len(ordered)
    )
    return ordered[: max(0, int(limit))]


def _window_mix(
    db: Session,
    user_id: int,
    limit: int,
    min_plays: int,
    newer_than: datetime | None = None,
    older_than: datetime | None = None,
    reason: str = "window",
) -> list[dict]:
    """Tracks with >= `min_plays` (by `user_id`), last played inside a window.

    `newer_than` / `older_than` are absolute cutoffs on `last_played_at`.
    Rows with no last-played timestamp are excluded: they are not evidence of
    anything, and including them would make every window mix identical.
    """
    plays = _plays()
    last_played = TrackAnnotation.last_played_at

    query = annotated_tracks(db, user_id).filter(
        plays >= int(min_plays),
        last_played.isnot(None),
    )
    if newer_than is not None:
        query = query.filter(last_played >= newer_than)
    if older_than is not None:
        query = query.filter(last_played < older_than)

    rows = query.order_by(plays.desc()).limit(2000).all()
    if not rows:
        return []

    out = []
    for track, track_plays, _rating, _starred, _last_played in rows:
        # Score keeps the heaviest rotation at the front; the scheduler's
        # diversity pass re-ranks from there.
        out.append(_to_dict(track, float(track_plays or 0), reason))
    return out[: max(0, int(limit))]


def select_on_repeat(db: Session, user_id: int, limit: int = 50, min_plays: int = 5,
                     within_days: int = 30, **_ignored) -> list[dict]:
    """What this user has actually been playing lately."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(within_days)))
    rows = _window_mix(db, user_id, limit, min_plays, newer_than=cutoff,
                       reason="on_repeat")
    if not rows:
        logger.info(
            "On Repeat: nothing with >=%d plays in the last %d days "
            "(is last_played_at populated? run a library sync)",
            min_plays, within_days,
        )
    return rows


def select_rewind(db: Session, user_id: int, limit: int = 50, min_plays: int = 5,
                  older_than_days: int = 30, within_days: int = 365,
                  **_ignored) -> list[dict]:
    """Heavy rotation from earlier in the year, but not from the last month."""
    now = datetime.utcnow()
    rows = _window_mix(
        db, user_id, limit, min_plays,
        newer_than=now - timedelta(days=max(1, int(within_days))),
        older_than=now - timedelta(days=max(1, int(older_than_days))),
        reason="rewind",
    )
    if not rows:
        logger.info("Rewind: no played tracks in the %d-%d day window", older_than_days, within_days)
    return rows


def select_rediscover(db: Session, user_id: int, limit: int = 50, min_plays: int = 3,
                      older_than_days: int = 365, **_ignored) -> list[dict]:
    """Old favourites untouched for a year or more."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(older_than_days)))
    rows = _window_mix(db, user_id, limit, min_plays, older_than=cutoff,
                       reason="rediscover")
    if not rows:
        logger.info("Rediscover: nothing last played before %s", cutoff.date())
    return rows


def select_unplayed(db: Session, user_id: int, limit: int = 50,
                    min_played_library: int = 50, rng=None, **_ignored) -> list[dict]:
    """Never-played (by this user) tracks by artists they already listen to.

    Gated on the library having real play history for this user: with a handful
    of plays the "unplayed" set is just the library, which is noise, not a
    recommendation. Restricting to artists with at least one play keeps it to
    music adjacent to what they actually listen to instead of everything left
    unripped-and-unheard.
    """
    rng = rng or seeded_rng("unplayed")
    plays = _plays()
    played_total = (
        _join_annotations(db.query(func.count(Track.id)), user_id)
        .filter(plays > 0)
        .scalar()
        or 0
    )
    if played_total < int(min_played_library):
        logger.info(
            "Unplayed: only %d played track(s) in the library (need %d) — skipping",
            played_total, min_played_library,
        )
        return []

    loved = [
        row[0]
        for row in _join_annotations(db.query(Track.artist_name), user_id)
        .filter(plays > 0, Track.artist_name.isnot(None))
        .group_by(Track.artist_name)
        .all()
    ]
    if not loved:
        return []

    rows = (
        annotated_tracks(db, user_id)
        .filter(
            # coalesce means "never annotated" and "annotated with 0 plays" are
            # the same thing here — both are unplayed for this user.
            plays == 0,
            Track.artist_name.in_(loved),
        )
        .limit(2000)
        .all()
    )
    if not rows:
        return []

    picked = weighted_sample(rows, lambda row: 1.0, limit, rng)
    return [_to_dict(row[0], 1.0, f"unplayed:{row[0].artist_name}") for row in picked]


_SELECTORS = {
    "deep_cuts": select_deep_cuts,
    "on_repeat": select_on_repeat,
    "rewind": select_rewind,
    "rediscover": select_rediscover,
    "unplayed": select_unplayed,
}


def select_mix(db: Session, user_id: int, kind: str, limit: int | None = None,
               rng=None, **overrides) -> list[dict]:
    """Dispatch to the selector for `kind`, applying MIX_SPECS defaults.

    Selection is always per user: `user_id` is forwarded so the mix reflects
    that listener's plays, not the dead shared `Track` columns.
    """
    kind = (kind or DEFAULT_MIX).strip().lower()
    selector = _SELECTORS.get(kind)
    if selector is None:
        logger.warning("Unknown mix kind '%s' — falling back to %s", kind, DEFAULT_MIX)
        kind = DEFAULT_MIX
        selector = _SELECTORS[kind]

    spec = dict(MIX_SPECS[kind])
    target = int(limit or spec.pop("limit", 50))
    spec.pop("label", None)
    spec.pop("description", None)
    spec.update({k: v for k, v in overrides.items() if v is not None})
    return selector(db, user_id, limit=target, rng=rng, **spec)
