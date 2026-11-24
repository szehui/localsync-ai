"""Discovery feed: recommend music that is NOT in the library.

This is the inverse of playlist generation. `services/resolver.py` treats an
entry that fails to resolve to a local song id as a *failure*. Here that failure
is the product.

Pipeline:

    seeds (top-played library tracks with an MBID)
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
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import DiscoveryOffer, DiscoveryState, Track
from app.services import lb_cache

logger = logging.getLogger(__name__)

# Seed scanning. About half of all recording MBIDs have no Labs neighbours, so
# we hand in a wide seed pool and stop early once enough *distinct* seeds have
# contributed. Cold ~1 call each; warm (7d cache) zero.
DEFAULT_SEED_COUNT = 30
MAX_SEED_COUNT = 60

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


def select_seeds(db: Session, limit: int = DEFAULT_SEED_COUNT) -> list[Track]:
    """Top-played tracks carrying an MBID, at most one per artist.

    One-per-artist matters: otherwise a single heavy-rotation artist (EXO at 193
    listens in live testing) consumes every seed slot and the candidate pool
    collapses onto one neighbourhood.
    """
    limit = max(1, min(limit, MAX_SEED_COUNT))
    rows = (
        db.query(Track)
        .filter(
            Track.recording_mbid.isnot(None),
            Track.recording_mbid != "",
            Track.play_count > 0,
        )
        .order_by(Track.play_count.desc(), Track.id)
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
                      lib_artists: set[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split candidates into (owned, artist_known, new_artist).

    `owned` is checked by recording MBID first (exact). A row whose MBID misses
    but whose artist is in the library is a gap-fill, not a discovery. A row
    with neither is genuinely new.
    """
    owned: list[dict] = []
    artist_known: list[dict] = []
    new_artist: list[dict] = []
    for cand in candidates:
        mbid = cand.get("recording_mbid")
        if mbid and mbid in lib_mbids:
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

def get_state(db: Session) -> DiscoveryState:
    """Fetch (or lazily create) the singleton state row."""
    state = db.query(DiscoveryState).filter(DiscoveryState.id == 1).first()
    if state is None:
        state = DiscoveryState(id=1)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def upsert_offers(db: Session, candidates: list[dict], bucket: str,
                  now: datetime | None = None) -> int:
    """Insert new offers, refresh the last_seen of known ones.

    Two rules matter:

    * A recording already known to the table is **never re-offered**. Its
      `last_seen` is bumped but its status is preserved, so a dismissed offer
      cannot resurrect itself and a saved one does not reappear.
    * Evidence is merged, not replaced, so a candidate that matches across
      several runs keeps accumulating its explanation.
    """
    stamp = now or datetime.utcnow()
    added = 0
    for cand in candidates:
        mbid = cand.get("recording_mbid")
        if not mbid:
            continue
        existing = db.query(DiscoveryOffer).filter(
            DiscoveryOffer.recording_mbid == mbid).first()
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


def offered_feed(db: Session, limit_artists: int = DEFAULT_FEED_ARTISTS) -> list[dict]:
    """Current feed: only `offered` rows, grouped by artist."""
    rows = (
        db.query(DiscoveryOffer)
        .filter(DiscoveryOffer.status == STATUS_OFFERED)
        .order_by(DiscoveryOffer.score.desc())
        .all()
    )
    return build_feed(rows, limit_artists)


# ── orchestration ────────────────────────────────────────────────────────────

def _candidate_from_labs_row(row: dict, seed: Track) -> dict:
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
        }],
    }


async def gather_labs_candidates(
    db: Session,
    client,
    seeds: list[Track],
    target_candidates: int = 0,
    min_live_seeds: int = 0,
    max_calls: int | None = None,
) -> tuple[list[dict], int, int]:
    """Expand seeds via Labs, using the 7d cache.

    Returns (candidates, api_calls, live_seeds).

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
                "Discovery: stopping seed scan at %d seeds (%d live, %d candidates)",
                live, live, len(candidates),
            )
            break
    return candidates, calls, live


async def refresh_feed(db: Session, client, seed_count: int = DEFAULT_SEED_COUNT) -> dict:
    """Run a full discovery refresh and persist offers. Returns a summary dict.

    Never raises: the state row records the failure so the UI can show it, and a
    partial run still persists whatever candidates were gathered.
    """
    state = get_state(db)
    state.last_status = "running"
    db.commit()

    summary = {"seeds_used": 0, "candidates_seen": 0, "offers_added": 0,
               "calls_made": 0, "status": "ok", "error": None}

    try:
        lib_mbids, lib_artists = library_index(db)
        seeds = select_seeds(db, seed_count)
        summary["seeds_used"] = len(seeds)
        if not seeds:
            raise RuntimeError(
                "no seeds available: the library has no played tracks with a "
                "recording MBID (run a full sync)"
            )

        raw, calls, live = await gather_labs_candidates(
            db, client, seeds,
            target_candidates=TARGET_CANDIDATES,
            min_live_seeds=MIN_LIVE_SEEDS,
            max_calls=MAX_SEED_CALLS,
        )
        summary["calls_made"] = calls
        summary["candidates_seen"] = len(raw)
        summary["live_seeds"] = live

        candidates = dedupe_candidates(raw)
        owned, artist_known, new_artist = triage_candidates(
            candidates, lib_mbids, lib_artists)

        added = upsert_offers(db, cap_per_artist(new_artist), NEW_ARTIST)
        added += upsert_offers(db, cap_per_artist(artist_known), ARTIST_KNOWN)
        summary["offers_added"] = added

        logger.info(
            "Discovery: %d seeds -> %d raw -> %d unique (%d owned, %d gap-fill, "
            "%d new) -> %d new offers, %d calls",
            len(seeds), len(raw), len(candidates), len(owned),
            len(artist_known), len(new_artist), added, calls,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Discovery refresh failed")
        summary["status"] = "error"
        summary["error"] = str(exc)

    state.last_run = datetime.utcnow()
    state.last_status = summary["status"]
    state.last_error = summary["error"]
    state.seeds_used = summary["seeds_used"]
    state.candidates_seen = summary["candidates_seen"]
    state.offers_added = summary["offers_added"]
    state.calls_made = summary["calls_made"]
    db.commit()
    return summary
