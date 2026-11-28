"""Discovery feed: recommend music that is NOT in the library.

This is the inverse of playlist generation. `services/resolver.py` treats an
entry that fails to resolve to a local song id as a *failure*. Here that failure
is the product.

Pipeline (per user — seeds and offers are personal):

    seeds (this user's top-played library tracks with an MBID)
      -> Labs similar-recordings per seed (names included, 7d cache)
      -> dedupe by recording MBID, merging evidence
      -> novelty triage against the library
             owned                    -> drop
             artist present, no album -> bucket "artist_known" (gap-fill)
             artist absent            -> bucket "new_artist"  (discovery)
      -> persist as DiscoveryOffer rows, deduped against past offers
      -> feed groups by artist

Why Labs and not CF for v1: Labs returns artist + title + release with an
explainable "because you played X". The CF endpoint returns MBIDs and a float
only, so its rows cannot be triaged by artist or displayed without paying for
MusicBrainz enrichment (1 req/sec, un-batchable). CF is a follow-up source.

Seed quality is the whole game. Labs returns nothing for roughly half of a real
user's top-played tracks (K-pop deep cuts, jazz covers and legacy pop scored 0
in live testing; mainstream/well-connected recordings returned 26-100 rows).
Dead seeds are cached as `[]`, so their cost is paid once and never again.

Seeds come from two pools, because one of them is badly biased:

* **Library play counts** — what Navidrome says this user played. High intent,
  but only as wide as the sync window (the last ~50 played albums), which
  measured 66 distinct artists against a 2,087-listen account.
* **ListenBrainz history** — every listen, including records the household does
  not own and therefore could never appear in the play-count pool. Sampled
  9/12 live against Labs versus 5/12 for play-count seeds, and 89 artists
  existed in history that the play-count pool could not reach.

The pool also rotates. Labs neighbours are cached 7 days and the offers ledger
never resurrects a row, so re-seeding the same recordings makes `offers_added`
zero forever — the feed froze at 255 rows for four days before this. Seeds used
recently are therefore pushed to the back of the queue.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.database import (
    Album,
    DiscoveryOffer,
    DiscoverySeedUse,
    DiscoveryState,
    Track,
    TrackAnnotation,
    User,
)
from app.services import lb_cache
from app.services.navidrome_native import NavidromeNative

logger = logging.getLogger(__name__)

# Seed scanning. About half of all recording MBIDs have no Labs neighbours, so
# we hand in a wide seed pool and stop early once enough *distinct* seeds have
# contributed. Cold ~1 call each; warm (7d cache) zero.
DEFAULT_SEED_COUNT = 30
MAX_SEED_COUNT = 60

# Share of the seed budget reserved for history-only artists. Without a reserve
# the play-count pool (sorted by plays) fills every slot and the history pool —
# the whole point — contributes nothing.
LB_SEED_QUOTA_SHARE = 0.5
LB_HISTORY_DAYS = 90
LB_HISTORY_COUNT = 1000

# A seed explored this recently is deprioritised, not banned: a pool smaller
# than the budget must still fill it.
ROTATION_DAYS = 14

# An offer nobody acted on eventually leaves the feed. Dismissed and saved rows
# are never expired — those are decisions, not staleness.
OFFER_TTL_DAYS = 45

# Stop scanning once this many candidates have been gathered from at least
# MIN_LIVE_SEEDS distinct seeds, or after MAX_SEED_CALLS network calls.
TARGET_CANDIDATES = 300
MIN_LIVE_SEEDS = 6
MAX_SEED_CALLS = 45

# Keep the feed readable: at most this many candidate tracks per artist get
# stored as evidence. The feed shows one row per artist regardless.
MAX_OFFERS_PER_ARTIST = 4

# Feed shows at most this many artist rows.
DEFAULT_FEED_ARTISTS = 40

NEW_ARTIST = "new_artist"
ARTIST_KNOWN = "artist_known"

STATUS_OFFERED = "offered"
STATUS_SAVED = "saved"
STATUS_DISMISSED = "dismissed"
STATUS_EXPIRED = "expired"

# Artists that carry no taste signal. Imported Last.fm history is full of them.
JUNK_ARTISTS = {"", "soundtrack", "various artists", "unknown artist"}


@dataclass
class Seed:
    """A recording to expand through Labs, from either seed pool.

    Same three attributes the old `Track` seeds exposed (`recording_mbid`,
    `artist_name`, `title`), so the candidate builder does not care where a
    seed came from — only the `source` is new, and it rides along into the
    evidence so a feed row can say whether it came from plays or from history.
    """
    recording_mbid: str
    artist_name: str
    title: str
    source: str  # "library" | "listenbrainz"
    play_count: int = 0
    listened_at: int = 0

    @property
    def artist_key(self) -> str:
        return normalize_artist(self.artist_name)


# ── small helpers ────────────────────────────────────────────────────────────

def normalize_artist(name: str | None) -> str:
    """Fold an artist name for comparison. Case and padding only.

    Deliberately conservative: aggressive normalization would merge genuinely
    distinct artists. The library does contain 'EXO'/'Exo' style splits, which
    this handles.
    """
    return (name or "").strip().lower()


def _load_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def library_index(db: Session) -> tuple[set[str], set[str]]:
    """(recording MBIDs owned, normalized artist names owned).

    Both sets are built from the local mirror, so triage costs zero API calls.
    MBID coverage of the library is ~55% and is all-or-nothing per album, which
    is exactly why the artist-name set exists as a second signal: an untagged
    album still contributes its artist name.
    """
    mbids = {
        m for (m,) in db.query(Track.recording_mbid).filter(
            Track.recording_mbid.isnot(None), Track.recording_mbid != ""
        ).distinct()
        if m
    }
    artists = {
        normalize_artist(a) for (a,) in db.query(Track.artist_name).distinct()
        if a and a.strip()
    }
    return mbids, artists


def mirror_release_groups(db: Session) -> set[str]:
    """Album MBIDs we have locally. Necessary, not sufficient — see below."""
    return {
        m for (m,) in db.query(Album.release_group_mbid).filter(
            Album.release_group_mbid.isnot(None),
            Album.release_group_mbid != "",
        ).distinct()
        if m
    }


async def owned_release_groups(db: Session, user_id: int) -> set[str]:
    """Album MBIDs the household actually owns, mirror plus server truth.

    The album table is a mirror of the library and an incomplete one — it held
    1,318 of the 2,656 album MBIDs Navidrome reports, because the library sync
    only writes what it walked and the MBID backfill only covers what it could
    resolve. Triage over a partial mirror answers "not owned" for albums that
    are sitting on disk, which is exactly the mistake a discovery feed cannot
    make: it would offer back the user's own records.

    So the set is the union of the mirror and a live read of the native API. The
    live read is one paginated walk of a local server (~4 calls) and a failure
    is non-fatal: the mirror is a strict subset of the truth, so falling back
    to it degrades coverage rather than corrupting the answer.
    """
    owned = mirror_release_groups(db)
    try:
        user = db.get(User, user_id)
        if user is None or not user.navidrome_url:
            return owned
        native = NavidromeNative(
            user.navidrome_url, user.username, user.navidrome_password)
        try:
            albums = await native.list_albums()
        finally:
            await native.close()
    except Exception as exc:  # noqa: BLE001 - never fail a feed over this
        logger.warning(
            "Discovery: live album MBIDs unavailable (%s); using the mirror only",
            exc,
        )
        return owned

    live = set()
    for album in albums:
        for key in ("mbzReleaseGroupId", "mbzAlbumId"):
            value = (album.get(key) or "").strip()
            if value:
                live.add(value)
    missing = len(live - owned)
    logger.info(
        "Discovery: owned release groups %d from server, %d from mirror, "
        "+%d the mirror did not know about", len(live), len(owned), missing,
    )
    return owned | live


def select_seeds(db: Session, user_id: int,
                 limit: int = DEFAULT_SEED_COUNT) -> list[Track]:
    """This user's top-played tracks carrying an MBID, at most one per artist.

    Play counts are personal (TrackAnnotation), not a property of the shared
    library row, so the join is not a lookup convenience: a track the household
    played 300 times but this user never touched is not a seed for THEIR
    discovery feed. Scoping by user_id is what makes the feed per-user at all —
    everything downstream (Labs neighbours, triage, offers) inherits the taste
    of whoever seeded it.

    One-per-artist matters: otherwise a single heavy-rotation artist (EXO at 193
    listens in live testing) consumes every seed slot and the candidate pool
    collapses onto one neighbourhood.
    """
    limit = max(1, min(limit, MAX_SEED_COUNT))
    rows = (
        db.query(Track)
        .join(
            TrackAnnotation,
            (TrackAnnotation.track_id == Track.id)
            & (TrackAnnotation.user_id == user_id),
        )
        .filter(
            Track.recording_mbid.isnot(None),
            Track.recording_mbid != "",
            TrackAnnotation.play_count > 0,
        )
        .order_by(TrackAnnotation.play_count.desc(), Track.id)
        .all()
    )
    seeds: list[Track] = []
    seen: set[str] = set()
    for track in rows:
        key = normalize_artist(track.artist_name)
        if key in seen:
            continue
        seen.add(key)
        seeds.append(track)
        if len(seeds) >= limit:
            break
    return seeds


def library_seed_pool(db: Session, user_id: int,
                      limit: int = MAX_SEED_COUNT) -> list[Seed]:
    """Play-count seeds as Seed objects, one per artist, in one query."""
    rows = (
        db.query(Track.recording_mbid, Track.artist_name, Track.title,
                 TrackAnnotation.play_count)
        .join(
            TrackAnnotation,
            (TrackAnnotation.track_id == Track.id)
            & (TrackAnnotation.user_id == user_id),
        )
        .filter(
            Track.recording_mbid.isnot(None),
            Track.recording_mbid != "",
            TrackAnnotation.play_count > 0,
        )
        .order_by(TrackAnnotation.play_count.desc(), Track.id)
        .all()
    )
    limit = max(1, min(limit, MAX_SEED_COUNT))
    seeds: list[Seed] = []
    seen: set[str] = set()
    for mbid, artist, title, plays in rows:
        key = normalize_artist(artist)
        if key in seen:
            continue
        seen.add(key)
        seeds.append(Seed(
            recording_mbid=mbid,
            artist_name=artist or "",
            title=title or "",
            source="library",
            play_count=int(plays or 0),
        ))
        if len(seeds) >= limit:
            break
    return seeds


def listenbrainz_seeds(listens: list[dict], limit: int = MAX_SEED_COUNT) -> list[Seed]:
    """Fold raw ListenBrainz listens into one seed per artist, most recent first.

    The pool is genuinely different from the play-count pool: listens include
    recordings the household does not own, and Labs expands *any* recording
    MBID, so history-only artists become seeds that were previously unreachable.
    Sampling measured 9/12 live against 5/12 for play-count seeds.

    Junk is dropped rather than passed on — an imported Last.fm history is full
    of `Soundtrack` and blank artist names, and a seed with no artist cannot be
    triaged or explained in the feed.
    """
    best: dict[str, Seed] = {}
    for listen in listens:
        mbid = (listen.get("recording_mbid") or "").strip()
        artist = (listen.get("artist") or "").strip()
        title = (listen.get("title") or "").strip()
        if not mbid or not artist or not title:
            continue
        key = normalize_artist(artist)
        if key in JUNK_ARTISTS:
            continue
        stamp = int(listen.get("listened_at") or 0)
        current = best.get(key)
        if current is None or stamp > current.listened_at:
            best[key] = Seed(
                recording_mbid=mbid,
                artist_name=artist,
                title=title,
                source="listenbrainz",
                listened_at=stamp,
            )
    ordered = sorted(best.values(), key=lambda s: s.listened_at, reverse=True)
    return ordered[:limit]


def merge_seed_pools(library: list[Seed], history: list[Seed],
                     limit: int = DEFAULT_SEED_COUNT) -> list[Seed]:
    """Combine both pools under a fixed quota for history-only artists.

    Concatenating and truncating would be wrong in a way that looks harmless:
    the library pool is ordered by play count and is large enough to fill the
    budget on its own, so the history pool would contribute nothing and the
    feature would silently keep its old bias. Half the budget is therefore
    reserved for history artists the library pool cannot reach; the rest is
    filled from plays, then topped up from history if plays run short.

    One seed per artist across the merged result: two seeds from the same artist
    explore the same neighbourhood and Labs calls are the expensive part.
    """
    limit = max(1, min(limit, MAX_SEED_COUNT))
    quota = max(1, round(limit * LB_SEED_QUOTA_SHARE))

    library_by_artist = {s.artist_key: s for s in library}
    history_only = [s for s in history if s.artist_key not in library_by_artist]
    history_rest = [s for s in history if s.artist_key in library_by_artist]

    picked: list[Seed] = []
    seen: set[str] = set()

    def take(seed: Seed) -> None:
        if len(picked) >= limit or seed.artist_key in seen:
            return
        seen.add(seed.artist_key)
        picked.append(seed)

    for seed in history_only[:quota]:
        take(seed)
    for seed in library:
        take(seed)
    for seed in history_rest:
        take(seed)
    for seed in history_only[quota:]:
        take(seed)
    return picked


def order_for_rotation(db: Session, user_id: int, seeds: list[Seed],
                       days: int = ROTATION_DAYS,
                       now: datetime | None = None) -> list[Seed]:
    """Push recently explored seeds to the back, keeping the order otherwise.

    Labs caches neighbours per recording for 7 days and the ledger never
    resurrects an offered row, so an unchanging seed list yields an unchanging
    feed. Rotating is what lets the next run walk into a new neighbourhood.

    Not an exclusion: a thin pool (a new user with 3 usable seeds) must still
    produce a feed, so recently-used seeds come last rather than disappearing.
    """
    stamp = now or datetime.utcnow()
    cutoff = stamp - timedelta(days=days)
    recent = {
        mbid for (mbid,) in db.query(DiscoverySeedUse.recording_mbid).filter(
            DiscoverySeedUse.user_id == user_id,
            DiscoverySeedUse.used_at >= cutoff,
        )
    }
    fresh = [s for s in seeds if s.recording_mbid not in recent]
    stale = [s for s in seeds if s.recording_mbid in recent]
    return fresh + stale


def record_seed_uses(db: Session, user_id: int, seeds: list[Seed],
                     now: datetime | None = None) -> None:
    """Remember which recordings this run explored, for the next rotation."""
    if not seeds:
        return
    stamp = now or datetime.utcnow()
    existing = {
        row.recording_mbid: row
        for row in db.query(DiscoverySeedUse).filter(
            DiscoverySeedUse.user_id == user_id,
            DiscoverySeedUse.recording_mbid.in_(
                [s.recording_mbid for s in seeds]),
        )
    }
    for seed in seeds:
        row = existing.get(seed.recording_mbid)
        if row is not None:
            row.used_at = stamp
            continue
        db.add(DiscoverySeedUse(
            user_id=user_id,
            recording_mbid=seed.recording_mbid,
            used_at=stamp,
        ))
    db.commit()


def expire_stale_offers(db: Session, user_id: int, days: int = OFFER_TTL_DAYS,
                        now: datetime | None = None) -> int:
    """Retire `offered` rows nobody acted on within the TTL.

    The ledger's rule is that an offer never resurrects, which is correct for a
    dismissal and wrong for indifference: without an expiry, a feed accumulates
    rows forever while the offers that would interest someone today are never
    generated. Only `offered` rows age out — `dismissed` and `saved` are
    decisions and stay put.
    """
    stamp = now or datetime.utcnow()
    cutoff = stamp - timedelta(days=days)
    expired = (
        db.query(DiscoveryOffer)
        .filter(
            DiscoveryOffer.user_id == user_id,
            DiscoveryOffer.status == STATUS_OFFERED,
            DiscoveryOffer.last_seen < cutoff,
        )
        .update({DiscoveryOffer.status: STATUS_EXPIRED},
                synchronize_session=False)
    )
    db.commit()
    if expired:
        logger.info("Discovery: user %s, expired %d stale offer(s)",
                    user_id, expired)
    return int(expired or 0)


# ── pure shaping functions (unit-tested directly) ────────────────────────────

def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    """Collapse repeats of the same recording across seeds.

    Scores are summed (several independent seeds agreeing is a stronger signal
    than one seed with a big raw count) and evidence lists are merged, because
    the evidence is what makes the feed explainable.
    """
    merged: dict[str, dict] = {}
    for cand in candidates:
        mbid = cand.get("recording_mbid")
        if not mbid:
            continue
        if mbid not in merged:
            merged[mbid] = {**cand, "score": float(cand.get("score") or 0.0),
                            "evidence": list(cand.get("evidence") or [])}
            continue
        existing = merged[mbid]
        existing["score"] = float(existing.get("score") or 0.0) + float(cand.get("score") or 0.0)
        existing["evidence"].extend(cand.get("evidence") or [])
        # Fill gaps in metadata from whichever row had it.
        for field in ("artist_name", "track_title", "release_name", "release_mbid"):
            if not existing.get(field) and cand.get(field):
                existing[field] = cand[field]
    return list(merged.values())


def triage_candidates(candidates: list[dict], lib_mbids: set[str],
                      lib_artists: set[str],
                      owned_release_groups: set[str] | None = None
                      ) -> tuple[list[dict], list[dict], list[dict]]:
    """Split candidates into (owned, artist_known, new_artist).

    Ownership is checked two ways, because either alone lies:

    * **Recording MBID** — exact, zero calls, but blind to a different edition
      of an album already held.
    * **Release-group MBID** — the album, which is what Lidarr and the library
      actually key on, and the only check that can see `(Deluxe Edition)` or a
      remaster as "already owned" rather than a gap.

    The release-group set must come from the *server*, not only from our album
    mirror: the mirror held 1,318 of Navidrome's 2,656 album MBIDs, so a
    mirror-only check silently offers half the library back as discoveries.

    A candidate whose artist is in the library but whose album is not is a
    gap-fill, not a discovery — that is the most actionable bucket, and
    branching on artist first would delete it.
    """
    owned_rgs = owned_release_groups or set()
    owned: list[dict] = []
    artist_known: list[dict] = []
    new_artist: list[dict] = []
    for cand in candidates:
        mbid = cand.get("recording_mbid")
        release_mbid = cand.get("release_mbid")
        if mbid and mbid in lib_mbids:
            owned.append(cand)
        elif release_mbid and release_mbid in owned_rgs:
            owned.append(cand)
        elif normalize_artist(cand.get("artist_name")) in lib_artists:
            artist_known.append(cand)
        else:
            new_artist.append(cand)
    return owned, artist_known, new_artist


def cap_per_artist(candidates: list[dict], per_artist: int = MAX_OFFERS_PER_ARTIST) -> list[dict]:
    """Limit candidates per artist, keeping the highest-scoring ones."""
    buckets: dict[str, list[dict]] = {}
    for cand in candidates:
        key = normalize_artist(cand.get("artist_name"))
        if not key:
            continue
        buckets.setdefault(key, []).append(cand)
    kept: list[dict] = []
    for rows in buckets.values():
        rows.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
        kept.extend(rows[:per_artist])
    return kept


def build_feed(offers: list[DiscoveryOffer], limit_artists: int = DEFAULT_FEED_ARTISTS) -> list[dict]:
    """Group offered rows by artist for display.

    Artist-level presentation is deliberate: 60 unknown tracks is an unusable
    wall, whereas "here are 12 artists, here is the one track that makes the
    case" is a decision a person can actually make — and it maps 1:1 to a Lidarr
    album add.
    """
    groups: dict[str, dict] = {}
    for offer in offers:
        key = normalize_artist(offer.artist_name) or offer.recording_mbid
        group = groups.get(key)
        if group is None:
            group = {
                "artist_name": offer.artist_name,
                "bucket": offer.bucket,
                "score": 0.0,
                "offers": [],
                "evidence": [],
            }
            groups[key] = group
        group["score"] += float(offer.score or 0.0)
        group["offers"].append({
            "id": offer.id,
            "recording_mbid": offer.recording_mbid,
            "track_title": offer.track_title,
            "release_name": offer.release_name,
            "release_mbid": offer.release_mbid,
            "score": float(offer.score or 0.0),
            "source": offer.source,
            "status": offer.status,
            "lidarr_album_id": offer.lidarr_album_id,
        })
        for item in _load_json(offer.evidence, []):
            if item not in group["evidence"]:
                group["evidence"].append(item)

    for group in groups.values():
        group["offers"].sort(key=lambda o: o["score"], reverse=True)
        # Most informative offer is the representative row for the artist.
        best = group["offers"][0]
        group["representative_offer_id"] = best["id"]
        group["representative_title"] = best["track_title"]
        group["representative_release"] = best["release_name"]
        group["offer_count"] = len(group["offers"])
        group["evidence_count"] = len(group["evidence"])

    ranked = sorted(
        groups.values(),
        key=lambda g: (g["evidence_count"], g["score"]),
        reverse=True,
    )
    return ranked[:limit_artists]


# ── persistence ──────────────────────────────────────────────────────────────

def get_state(db: Session, user_id: int) -> DiscoveryState:
    """Fetch (or lazily create) this user's discovery state row.

    The row is per-user: the refresh job reads that user's own play history, so
    a shared row meant the Discover page showed whichever user refreshed last —
    including their error text, next to someone else's counts.
    """
    state = db.query(DiscoveryState).filter(
        DiscoveryState.user_id == user_id).first()
    if state is None:
        state = DiscoveryState(user_id=user_id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def upsert_offers(db: Session, user_id: int, candidates: list[dict], bucket: str,
                  now: datetime | None = None) -> int:
    """Insert new offers for one user, refresh the last_seen of known ones.

    Two rules matter:

    * A recording already offered to *this* user is **never re-offered**. Its
      `last_seen` is bumped but its status is preserved, so a dismissed offer
      cannot resurrect itself and a saved one does not reappear.
    * Evidence is merged, not replaced, so a candidate that matches across
      several runs keeps accumulating its explanation.

    Matching is on (user_id, recording_mbid), not recording_mbid alone: another
    user's dismissal is their opinion, and must not stop this user being
    offered the same recording.
    """
    stamp = now or datetime.utcnow()
    added = 0
    for cand in candidates:
        mbid = cand.get("recording_mbid")
        if not mbid:
            continue
        existing = db.query(DiscoveryOffer).filter(
            DiscoveryOffer.user_id == user_id,
            DiscoveryOffer.recording_mbid == mbid,
        ).first()
        evidence = cand.get("evidence") or []
        if existing is not None:
            existing.last_seen = stamp
            merged = _load_json(existing.evidence, [])
            for item in evidence:
                if item not in merged:
                    merged.append(item)
            existing.evidence = json.dumps(merged)
            existing.score = max(float(existing.score or 0.0),
                                 float(cand.get("score") or 0.0))
            continue

        db.add(DiscoveryOffer(
            user_id=user_id,
            recording_mbid=mbid,
            artist_name=cand.get("artist_name"),
            track_title=cand.get("track_title"),
            release_name=cand.get("release_name"),
            release_mbid=cand.get("release_mbid"),
            source=cand.get("source") or "labs",
            score=float(cand.get("score") or 0.0),
            evidence=json.dumps(evidence),
            bucket=bucket,
            status=STATUS_OFFERED,
            first_seen=stamp,
            last_seen=stamp,
        ))
        added += 1
    db.commit()
    return added


def offered_rows(db: Session, user_id: int) -> list[DiscoveryOffer]:
    """This user's `offered` rows, best score first."""
    return (
        db.query(DiscoveryOffer)
        .filter(
            DiscoveryOffer.user_id == user_id,
            DiscoveryOffer.status == STATUS_OFFERED,
        )
        .order_by(DiscoveryOffer.score.desc())
        .all()
    )


def offered_feed(db: Session, user_id: int,
                 limit_artists: int = DEFAULT_FEED_ARTISTS) -> list[dict]:
    """Current feed for one user: only `offered` rows, grouped by artist."""
    return build_feed(offered_rows(db, user_id), limit_artists)


# ── orchestration ────────────────────────────────────────────────────────────

def _candidate_from_labs_row(row: dict, seed: Seed) -> dict:
    return {
        "recording_mbid": row.get("recording_mbid"),
        "artist_name": row.get("artist_credit_name"),
        "track_title": row.get("recording_name"),
        "release_name": row.get("release_name"),
        "release_mbid": row.get("release_mbid"),
        "source": "labs",
        "score": float(row.get("score") or 0.0),
        "evidence": [{
            "seed_artist": seed.artist_name,
            "seed_title": seed.title,
            # Track seeds (the pre-history pool) carry no source; they are the
            # library pool by definition.
            "seed_source": getattr(seed, "source", "library"),
        }],
    }


async def _recent_listens(client, days: int = LB_HISTORY_DAYS,
                          count: int = LB_HISTORY_COUNT) -> list[dict]:
    """This user's ListenBrainz history, or [] if it cannot be read.

    History is an *additive* seed source: a missing token, a 429 or a client
    built without the method must degrade the feed to play-count seeds, never
    fail the run. The scheduler's playlist path makes the same call and treats a
    thin history as a normal condition rather than an error.
    """
    if client is None or not hasattr(client, "get_recent_listens"):
        return []
    try:
        listens = await client.get_recent_listens(days=days, count=count)
    except Exception as exc:  # noqa: BLE001 - never fail a feed over history
        logger.warning(
            "Discovery: ListenBrainz history unavailable (%s); seeding from "
            "play counts only", exc,
        )
        return []
    return list(listens or [])


async def gather_labs_candidates(
    db: Session,
    user_id: int,
    client,
    seeds: list[Seed],
    target_candidates: int = 0,
    min_live_seeds: int = 0,
    max_calls: int | None = None,
) -> tuple[list[dict], int, int, list[str]]:
    """Expand one user's seeds via Labs, using the 7d cache.

    Returns (candidates, api_calls, live_seeds, scanned_mbids).

    ``scanned_mbids`` is what the rotation ledger should record: the scan stops
    as soon as enough seeds have contributed, so recording the whole selected
    list would mark seeds as "explored" that were never expanded, and the next
    run would deprioritise them for nothing.

    ``user_id`` is threaded for a uniform per-user pipeline; the neighbours of
    an MBID are a property of the recording, not of who asked, so the cache that
    makes this affordable stays global (lb_similar_cache). The *seeds* are what
    make the run personal, and they arrive already scoped to the user.

    Roughly half of all recording MBIDs have no Labs neighbours at all, so a
    fixed seed count produces a feed dominated by whichever two or three seeds
    happened to have data. Callers should pass ``target_candidates`` /
    ``min_live_seeds`` and hand in more seeds than they need; scanning stops as
    soon as enough *distinct* seeds have contributed. Results are cached, so the
    extra scanning is a one-time cost.
    """
    candidates: list[dict] = []
    calls = 0
    live = 0
    scanned: list[str] = []
    for seed in seeds:
        mbid = seed.recording_mbid
        if not mbid:
            continue
        rows = lb_cache.get_cached_similar(db, mbid)
        if rows is None:
            if max_calls is not None and calls >= max_calls:
                break
            try:
                rows = await client.get_similar_recordings(mbid)
            except Exception as exc:  # noqa: BLE001 - one bad seed must not kill the run
                logger.warning("Discovery: Labs failed for seed %s: %s", mbid, exc)
                continue
            calls += 1
            lb_cache.put_similar(db, mbid, rows)
        scanned.append(mbid)
        if rows:
            live += 1
        for row in rows:
            if not row.get("recording_mbid"):
                continue
            candidates.append(_candidate_from_labs_row(row, seed))

        if (
            target_candidates
            and len(candidates) >= target_candidates
            and live >= min_live_seeds
        ):
            logger.info(
                "Discovery: stopping seed scan at %d scanned (%d live, "
                "%d candidates)", len(scanned), live, len(candidates),
            )
            break
    return candidates, calls, live, scanned


async def refresh_feed(db: Session, user_id: int, client,
                       seed_count: int = DEFAULT_SEED_COUNT) -> dict:
    """Run a full discovery refresh for one user and persist their offers.

    Four stages, each of which fixes a way this used to silently stall:

    1. Retire stale offers, so indifference does not permanently occupy the
       ledger that "never resurrects".
    2. Build seeds from *both* pools — library plays and ListenBrainz history —
       under a quota that guarantees history-only artists get in.
    3. Rotate: push seeds explored in the last ROTATION_DAYS to the back of each
       pool *before* the quota merge, or every run re-expands the same
       recordings from the 7d Labs cache and finds nothing new. Rotating after
       the merge is a no-op: the merge has already truncated to the budget.
    4. Triage against server-side ownership, not just the album mirror.

    Never raises: the state row records the failure so the UI can show it, and a
    partial run still persists whatever candidates were gathered.
    """
    state = get_state(db, user_id)
    state.last_status = "running"
    db.commit()

    summary = {"seeds_used": 0, "lb_seeds_used": 0, "candidates_seen": 0,
               "offers_added": 0, "offers_expired": 0, "calls_made": 0,
               "status": "ok", "error": None}

    try:
        summary["offers_expired"] = expire_stale_offers(db, user_id)

        lib_mbids, lib_artists = library_index(db)
        owned_rgs = await owned_release_groups(db, user_id)

        library_seeds = order_for_rotation(
            db, user_id, library_seed_pool(db, user_id))
        history_seeds: list[Seed] = []
        listens = await _recent_listens(client)
        if listens:
            history_seeds = order_for_rotation(
                db, user_id, listenbrainz_seeds(listens))
        # Rotate *before* the quota merge. Merging first truncates to the seed
        # budget, leaving rotation nothing to reorder — the second run then
        # re-expands the exact seeds the first one used and finds nothing new.
        seeds = merge_seed_pools(library_seeds, history_seeds, seed_count)

        summary["seeds_used"] = len(seeds)
        summary["lb_seeds_used"] = sum(
            1 for s in seeds if s.source == "listenbrainz")
        if not seeds:
            raise RuntimeError(
                "no seeds available: this user has no played tracks with a "
                "recording MBID and no ListenBrainz history (listen to "
                "something, or run a full sync)"
            )

        raw, calls, live, scanned = await gather_labs_candidates(
            db, user_id, client, seeds,
            target_candidates=TARGET_CANDIDATES,
            min_live_seeds=MIN_LIVE_SEEDS,
            max_calls=MAX_SEED_CALLS,
        )
        summary["calls_made"] = calls
        summary["candidates_seen"] = len(raw)
        summary["live_seeds"] = live

        candidates = dedupe_candidates(raw)
        owned, artist_known, new_artist = triage_candidates(
            candidates, lib_mbids, lib_artists, owned_rgs)

        added = upsert_offers(db, user_id, cap_per_artist(new_artist), NEW_ARTIST)
        added += upsert_offers(db, user_id, cap_per_artist(artist_known),
                               ARTIST_KNOWN)
        summary["offers_added"] = added

        record_seed_uses(db, user_id,
                         [s for s in seeds if s.recording_mbid in set(scanned)])

        logger.info(
            "Discovery: user %s, %d seeds (%d from history) -> %d raw -> "
            "%d unique (%d owned, %d gap-fill, %d new) -> %d new offers, "
            "%d expired, %d calls",
            user_id, len(seeds), summary["lb_seeds_used"], len(raw),
            len(candidates), len(owned), len(artist_known), len(new_artist),
            added, summary["offers_expired"], calls,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Discovery refresh failed")
        summary["status"] = "error"
        summary["error"] = str(exc)

    state.last_run = datetime.utcnow()
    state.last_status = summary["status"]
    state.last_error = summary["error"]
    state.seeds_used = summary["seeds_used"]
    state.lb_seeds_used = summary["lb_seeds_used"]
    state.candidates_seen = summary["candidates_seen"]
    state.offers_added = summary["offers_added"]
    state.offers_expired = summary["offers_expired"]
    state.calls_made = summary["calls_made"]
    db.commit()
    return summary
