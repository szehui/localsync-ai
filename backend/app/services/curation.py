"""Playlist curation primitives — pure functions, no DB and no network.

These are the pieces cribbed from the way Aurral builds its mixes: seeded,
reproducible shuffling; weighted sampling without replacement; and a greedy
diversity pass that stops one artist or genre from taking over a playlist.

Nothing here touches the database or the network, so every function is
unit-testable with plain lists. Callers in `scheduler.py` and `mixes.py` do the
DB lookups and hand the results in.
"""
import hashlib
import logging
import math
import random

logger = logging.getLogger(__name__)

# Scores live on a 0..BASE_SCORE_SPAN scale (see `normalize_scores`), so the
# penalties below are meaningful fractions of the range rather than noise.
BASE_SCORE_SPAN = 10.0

# Penalties applied per prior occurrence of a value already in the selection.
# Expressed as a fraction of BASE_SCORE_SPAN: 4.0 of 10 is roughly "four ranks"
# when scores are spread evenly across a pool of ~40, which is what lifts a
# fresh artist above the second-best track of one already used. The value has to
# be this large relative to the span or a pool where one artist owns the top of
# the list still produces back-to-back tracks by that artist.
ARTIST_PENALTY = 4.0
GENRE_PENALTY = 2.5

# Aging: a track offered recently loses score, decaying to zero across the
# horizon. Recent offerings are penalised hardest — the point is to stop the
# same tracks leading the playlist two runs in a row, not to punish old ones.
AGING_CAP = 3.0
AGING_HORIZON_DAYS = 30.0

# Published playlists shorter than this are not worth shipping; the caller
# usually keeps the previous run's playlist instead.
MIN_PLAYLIST_TRACKS = 10


def seeded_rng(*parts) -> random.Random:
    """Deterministic RNG derived from `parts`.

    Uses md5 rather than `hash()` because Python salts string hashing per
    process, which would make the same playlist name produce a different
    playlist after every restart. Everything that must be reproducible across
    processes goes through here.
    """
    key = "|".join(str(p) for p in parts)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def weighted_sample(items, weight_fn, k: int, rng=None) -> list:
    """Sample `k` items without replacement, biased by weight.

    Efraimidis–Spirakis: give each item the key ``log(U) / w`` and keep the
    largest keys. One pass, no rejection loop, and the result is deterministic
    for a given `rng`. Zero/negative weights are treated as "never pick" rather
    than raising, so a track with play_count 0 simply sorts last under a
    familiarity weighting instead of breaking the run.
    """
    rng = rng or random
    k = max(0, int(k))
    if k == 0:
        return []

    keyed = []
    for item in items:
        try:
            weight = float(weight_fn(item))
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            keyed.append((-math.inf, rng.random(), item))
            continue
        # rng.random() is in [0, 1); log(0) is undefined, so shift off zero.
        u = rng.random() or 1e-12
        keyed.append((math.log(u) / weight, rng.random(), item))

    keyed.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in keyed[:k]]


def familiarity_weight(play_count: int, mode: str = "neutral") -> float:
    """Weight for a track given its play count.

    ``deep``     — favour the least-played tracks (Deep Cuts)
    ``familiar`` — favour the most-played (On Repeat)
    ``neutral``  — flat, i.e. plain uniform sampling

    Returns 0.0 for ``deep`` mode when the track has never been played, which
    removes it from the draw entirely: an unplayed track cannot be a "deep cut"
    of an artist you love.
    """
    plays = max(0, int(play_count or 0))
    if mode == "deep":
        if plays <= 0:
            return 0.0
        return 1.0 / (1.0 + plays)
    if mode == "familiar":
        return 1.0 + math.log1p(plays)
    return 1.0


def aging_penalty(days_since_offered, cap: float = AGING_CAP,
                  horizon_days: float = AGING_HORIZON_DAYS) -> float:
    """Score to subtract for a track last offered `days_since_offered` ago.

    Decays linearly from `cap` at zero days to 0 at `horizon_days`, so a track
    served yesterday is pushed to the back of the queue while one from last
    month competes normally again. ``None`` (never offered) costs nothing.

    Note the direction: the penalty *falls* as the track gets older, which is
    the opposite of a "freshness boost" — this is a repeat suppressant.
    """
    if days_since_offered is None:
        return 0.0
    try:
        days = float(days_since_offered)
    except (TypeError, ValueError):
        return 0.0
    if days < 0:
        return float(cap)
    if horizon_days <= 0:
        return float(cap)
    if days >= horizon_days:
        return 0.0
    return cap * (1.0 - days / horizon_days)


def apply_artist_cap(ids, artist_of, cap: int) -> list:
    """Keep at most `cap` tracks per artist, preserving input order.

    `artist_of` maps an id to an artist name; ids it cannot resolve are kept
    (an unknown artist is not a reason to drop the track).
    """
    if cap is None or cap <= 0:
        return list(ids)
    seen: dict = {}
    out = []
    for sid in ids:
        artist = (artist_of(sid) or "").strip()
        if not artist:
            out.append(sid)
            continue
        count = seen.get(artist, 0)
        if count >= cap:
            continue
        seen[artist] = count + 1
        out.append(sid)
    return out


def interleave(lists) -> list:
    """Round-robin several lists, preserving each list's internal order.

    Used to stop a per-artist batch (3 Deep Cuts from one artist) from landing
    as one contiguous block.
    """
    queues = [list(lst) for lst in lists]
    out = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


def normalize_scores(candidates, span: float = BASE_SCORE_SPAN) -> list:
    """Return `candidates` with scores remapped to 0..`span` by rank.

    Selectors hand in raw scores on wildly different scales (play counts in the
    hundreds, a 1/(1+plays) familiarity weight near zero). Diversity penalties
    are absolute, so without normalising a 2.6-point artist penalty would be
    invisible next to a 400-play track and overpowering next to a 0.2 one.
    Rank normalisation makes the penalty mean the same thing regardless of the
    score distribution upstream.

    Ties keep their input order (sort is stable).
    """
    out = [dict(c) for c in candidates if c and c.get("id")]
    if not out:
        return []
    ordered = sorted(out, key=lambda c: float(c.get("score") or 0.0), reverse=True)
    n = len(ordered)
    for rank, candidate in enumerate(ordered):
        candidate["score"] = span * (1.0 - rank / max(1, n - 1)) if n > 1 else span
    return out


def diversify(candidates, limit: int, rng=None, artist_penalty: float = ARTIST_PENALTY,
              genre_penalty: float = GENRE_PENALTY, artist_penalty_cap: int = 3,
              genre_window: int = 3) -> list:
    """Greedy selection that penalises repeating an artist or a genre.

    `candidates` is a list of dicts with ``id``, an optional ``artist``,
    optional ``genre``, and a ``score`` on the `BASE_SCORE_SPAN` scale. Returns
    candidate ids in selection order.

    Two different mechanisms, because the two groupings behave differently:

    * **Artist** — penalised cumulatively but capped: the k-th repeat costs
      ``artist_penalty`` for k up to ``artist_penalty_cap``. Uncapped, an artist
      owning half the pool would be pushed below irrelevant tracks by the end.
    * **Genre** — penalised only within the last ``genre_window`` picks. Genres
      are coarse ("Rock" can be a third of a library), so a cumulative genre
      penalty would effectively ban the dominant genre after 10 tracks instead
      of merely interleaving it.

    With both penalties at 0 this degrades to a plain sort by score. Ties are
    broken by the seeded `rng`, so equally-ranked tracks shuffle between runs
    but stay stable within one.
    """
    remaining = [c for c in candidates if c and c.get("id")]
    limit = max(0, int(limit))
    if limit == 0 or not remaining:
        return []

    chosen: list = []
    seen_artists: dict = {}
    recent_genres: list = []

    # Tie-break key fixed per candidate upfront so the seeded rng is consumed a
    # predictable number of times (keeps reproducibility obvious).
    for c in remaining:
        c["_tie"] = rng.random() if rng is not None else 0.0

    while remaining and len(chosen) < limit:
        best_index = 0
        best_score = -math.inf
        for i, c in enumerate(remaining):
            score = float(c.get("score") or 0.0)
            artist = (c.get("artist") or "").strip()
            genre = (c.get("genre") or "").strip()
            if artist:
                repeats = min(seen_artists.get(artist, 0), artist_penalty_cap)
                score -= artist_penalty * repeats
            if genre and genre in recent_genres:
                score -= genre_penalty
            if score > best_score + 1e-9 or (
                abs(score - best_score) <= 1e-9
                and c["_tie"] > remaining[best_index]["_tie"]
            ):
                best_index = i
                best_score = score

        pick = remaining.pop(best_index)
        chosen.append(pick["id"])
        artist = (pick.get("artist") or "").strip()
        genre = (pick.get("genre") or "").strip()
        if artist:
            seen_artists[artist] = seen_artists.get(artist, 0) + 1
        if genre:
            recent_genres.append(genre)
            if genre_window > 0:
                del recent_genres[:-genre_window]

    return chosen
