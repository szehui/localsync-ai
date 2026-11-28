# Music Requests ("Seerr for music") Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make localsync-ai a request front-end for Lidarr — a searchable music catalog
where a household member can find an album (or an artist's discography), request it, and
watch it move from pending to playable in Navidrome.

**Architecture:** MusicBrainz release groups are the unit of everything. A request stores
a `release_group_mbid`; Lidarr is asked to resolve that MBID directly
(`album/lookup?term=mbid:<uuid>`, verified live on 3.1.6.5079) and the existing
single-album monitoring collapse is reused. Title matching stops being the request path
and stays only as the YouTube-import fallback. Catalog reads come from MusicBrainz +
Cover Art Archive + ListenBrainz rails, cached locally under a 1 req/s budget. Status is
reconciled from Lidarr album statistics/queue/history plus Navidrome's release-group MBID,
so a request can fail loudly instead of vanishing.

**Tech Stack:** FastAPI, SQLAlchemy 2.x ORM, SQLite (inline migrations in `init_db()`),
APScheduler, httpx, React + TypeScript, pytest.

---

## Context an implementer must have

Read these before starting. Do not guess.

| File | Why |
| --- | --- |
| `backend/app/models/database.py` | All models; `init_db()` holds the inline migrations |
| `backend/app/services/lidarr.py` | The add path, `_add_album_core`, `_monitor_only`, `collapse_artist_monitoring` |
| `backend/app/routers/discovery.py` | Existing "add to Lidarr" endpoint; the pattern to replace |
| `backend/app/services/navidrome.py` | Subsonic client used by sync |
| `backend/app/services/sync.py` | `full_sync`; albums are synced from `getAlbumList2` |
| `backend/app/services/listenbrainz.py` | Rate-limited client pattern to copy |
| `backend/app/services/users.py` | Per-user config lookups |
| `backend/app/routers/auth.py` | `get_current_user`, `require_owner` |
| `frontend/src/App.tsx` | `TABS` array and router |
| `docs/research/2026-11-27-request-system.md` | Probe evidence behind every claim below |

**Test command** (from `backend/`): `./venv/bin/python -m pytest app/tests -q`
Baseline: **551 passing as of 2026-11-27**. Do not drop below it without explaining why.

**Style rules already in this repo — follow them:**
- Migrations are idempotent `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX
  IF NOT EXISTS` blocks inside `init_db()`. No Alembic, no migration files.
- `Base.metadata.create_all()` does not alter existing tables. Every new column needs an
  explicit `ALTER TABLE` in `init_db()` **and** a `CREATE INDEX` if the model says
  `index=True`. Tests that build a fresh schema cannot catch this — see
  `test_migration_upgrade_path.py`.
- Tests use real in-memory SQLite (`create_engine("sqlite:///:memory:")` +
  `Base.metadata.create_all`) when queries are under test. Fake clients for HTTP.
- `datetime.utcnow()` (naive UTC) everywhere. No tz-aware datetimes.
- Comments explain *why*, not *what*.
- All money-free, household-scale: no pagination frameworks, no background task queues.

---

## Decisions taken

**Product (answered by the user, 2026-11-27):**

1. **Approval:** every request starts `pending`. Each user gets an `auto_approve` flag,
   **default false**; the owner can flip it per user. Owner can also approve/deny after
   the fact.
2. **Granularity:** album **and** artist discography requests, available to everyone.
3. **Landing page:** new-releases + trending rails ship in v1.

**Technical:**

4. **Release-group MBID is the request key.** Lidarr album ids are not stable (metadata
   refreshes delete and re-create rows) and must never be a durable key.
5. **Artist requests expand into album requests.** They do **not** call `POST /artist`
   with `monitor: "all"`. An artist add monitors the entire discography, and a background
   `RefreshArtist` keeps re-monitoring; per-album requests keep the existing
   collapse-once-per-artist contract and give per-album status and failure isolation.
6. **MusicBrainz direct, not Lidarr search.** Lidarr's lookup is MusicBrainz behind
   SkyHook, capped ~10 rows and 503-prone; going direct gives paging, scores, browse
   filters and no dependency on Lidarr for browsing. Lidarr is still used as the *add*
   resolver by MBID.
7. **Deezer is not in the request path.** It has no MBIDs (probed: album and artist
   objects contain none). Optional previews later; not v1.
8. **Availability needs two signals.** Navidrome `mbzReleaseGroupId` (measured **36.8%**
   on the real 3,626-album library) plus the Lidarr oracle (Task 1.10) takes it to
   **70.2%**; a MusicBrainz tail pass adds ~5 more. The name+track-count fallback covers
   the floor, which is 716 single-track rips and 135 `[Unknown Album]` tags. A false
   "available" is worse than "finishing up".

---

## Known traps (all verified — do not re-litigate)

1. **MusicBrainz is 1 req/s** (`X-RateLimit-Limit: 400`/window) and returns **503** under
   burst. Serialise every call; retry 429/502/503/504 with 1/3/6s backoff.
2. **A User-Agent is mandatory** — `localsync-ai/<version> ( https://github.com/szehui/localsync-ai )`.
   Default library UAs are blocked with no useful error.
3. **`album/lookup?term=mbid:<uuid>` returns exactly one row** and it is the only lookup
   form that cannot pick the wrong artist. Still assert `foreignAlbumId == requested`
   before adding; never `results[0]`.
4. **Navidrome Subsonic `musicBrainzId` is the release MBID**, not the release group.
   Lidarr's `foreignAlbumId` is the release group. Matching those two is silently always
   wrong. Use the native API's `mbzReleaseGroupId`.
5. **`POST /album` monitors the whole discography as a side effect.** `addOptions.monitor:
   "none"` only clears the artist flag. Always set `monitorNewItems: "none"` on the artist
   and collapse with a converge-verify loop.
6. **Collapse once per artist per batch.** `add_single_album` in a loop un-monitors its
   own earlier adds (measured: 34 of 88).
7. **ListenBrainz popularity endpoints are POST-only** and take `release_group_mbids` /
   `artist_mbids` (GET → 405, wrong field → 400).
8. **MusicBrainz search is token-based, not typo-tolerant.** A typo query returns
   six-figure `count` with the right answer ranked #1 by `score`. Trust ordering, show
   metadata, and cap `limit` at 25.
9. **Various Artists and Korean-titled albums ARE addable by MBID** (Phase 0, live).
   The old "unreachable" note described *title* lookup failing to bridge a script or
   an artist-credit difference — not a limitation of MusicBrainz or Lidarr. What
   remains is release-group spelling: the UI must show year, type and localized titles
   so the user picks the right RG. There is no refused-at-creation category; the
   failure state to design for is "Lidarr could not resolve this MBID at all".

---

# Phase 0 — Spikes (DONE 2026-11-27)

**Outcome: the design holds. See "Phase 0 spike results" in
`docs/research/2026-11-27-request-system.md` for the full evidence.**

- MBID add + collapse works, including the Various Artists soundtrack and the
  Korean-titled `싸다9`. Both were already in Lidarr, which means the old
  "unreachable" note was a title-lookup artifact.
- Real release-group MBID coverage is **36.8%** (1,334 / 3,626 albums); the name +
  track-count fallback is required.
- `POST /album` creates the artist's discography metadata as a side effect, so the
  reconciler must never treat "an unmonitored row exists" as "added".
- K-pop is solved by MB-native mechanisms: search normalization, an `also_known_as`
  list from the release-group's releases, and artist-first browse (Task 1.11).

Tasks 0.1–0.3 below are kept as the record of what was run.

No app code. Scripts in `scripts/spikes/`, `docs/research/`. **Stop and report if a
spike fails.**

### Task 0.1: Prove MBID add end-to-end, including the known-unreachable cases

**Objective:** Confirm `mbid:` lookup + `POST /album` + collapse works for a normal album,
a Various Artists soundtrack, and a Korean-titled album.

**Files:**
- Create: `scripts/spikes/mbid_add.py`
- Modify: `docs/research/2026-11-27-request-system.md` (append a "Spike results" section)

**Step 1:** Script reads `LIDARR_URL` / `LIDARR_API_KEY` from env, takes
`--mbid` and `--dry-run`, and does:
`GET album/lookup?term=mbid:<uuid>` → assert one row and `foreignAlbumId` equal →
`POST /album` with `monitored: true`, `addOptions: {addType: manual, searchForNewAlbum:
false}`, artist patched with `monitorNewItems: "none"`, `monitored: false`,
`addOptions.monitor: "none"` → `PUT album/monitor` keep-set → re-read and print
`monitored` for the artist's albums.

**Step 2:** Run against:
- `aa997ea0-2936-40bd-884d-3af8a0e064dc` (Daft Punk — Random Access Memories)
- a Shang-Chi / KPop Demon Hunters release group (look up its MBID live via MB search)
- `PSY 9th` (Korean title in Lidarr; find its MBID via MB search `artist:PSY AND releasegroup:9th`)

**Step 3:** Record for each: resolved / not resolved, artist created, monitored-album set
after collapse, and whether a search returns releases.

**Acceptance:** All three either add cleanly or produce a specific, documented failure
mode. **Result: all three added; two were already present. No refused-at-creation
category is needed** — see the Phase 0 results section.

**Commit:** `chore(spike): prove Lidarr MBID add path`

---

### Task 0.2: Measure release-group MBID coverage on the real library

**Objective:** Replace the 5/12 sample with a real number, and prove the native API walk.

**Files:**
- Create: `scripts/spikes/navidrome_rg_coverage.py`

**Step 1:** Read the owner's Navidrome URL/user/password from the localsync DB (never
print the password), `POST /auth/login`, then page `/api/album?_start=&_end=200` with
`x-nd-authorization: Bearer <jwt>` until fewer than 200 rows return.

**Step 2:** Count albums, `mbzReleaseGroupId` present, `mbzAlbumId` present, and
`mbzAlbumId != mbzReleaseGroupId` (they should differ — that is the trap in Known traps
#4). Print totals and percentages.

**Step 3:** Save the JSON of `{album_id, name, mbzReleaseGroupId}` to
`/tmp/rg-coverage.json` for reuse in Task 1.10.

**Acceptance:** A percentage and a page count. If native-API auth fails, stop and report
— Task 1.10 depends on this working.
**Result: 3,626 albums over 19 pages; 1,334 with a release-group MBID (36.8%).**

**Commit:** `chore(spike): measure Navidrome release-group MBID coverage`

---

### Task 0.3: Confirm discography browse semantics

**Objective:** Pin down how to enumerate a requestable discography.

**Files:** none (curl + notes in the research doc)

**Step 1:** Against `artist=056e4f3e-d505-4dad-8ec1-d04f521cbb56` (Daft Punk):
`GET /ws/2/release-group?artist=<mbid>&type=album&limit=100&inc=artist-credits`
and the same with `type=ep`. Note counts, paging (`offset`), and whether `type` can be
repeated in one call.

**Step 2:** For a prolific artist (e.g. `artist:ENHYPEN`), confirm the total and that
`offset` pages correctly.

**Step 3:** Record the filter rule that yields "studio albums": primary type Album, no
secondary types. Check whether soundtrack albums (secondary type `Soundtrack`) would be
excluded by that rule — they should be *included by choice*, not by accident.

**Acceptance:** A written rule + counts for two artists.
**Result: `type` works one value per call (repeat → 503); album/EP fit in one page,
singles cap at 100 of 145. Daft Punk 51 albums / 8 EPs. ENHYPEN name collision means
artist search must not take `results[0]`.**

---

# Phase 1 — Catalog (read-only)

**Status: complete (2026-11-27).** 747 tests passing (196 new). See DEVLOG for the
measured decisions. Deviations from this plan, all deliberate:

- The `requests` table was defined in this phase, not Phase 2 — catalog cards must
  annotate "already requested", and its partial unique index is the concurrency
  guard for the request button. Phase 2 Task 2.1 therefore only adds `RequestVote`,
  `Blocklist` and the two `users` columns.
- `sync_album_mbids` also backfills `albums.year` from Navidrome's `maxYear`: the
  Subsonic album object has no `year` field, so every row was NULL and the date
  tiebreaker in the backfill would have been dead code.
- Album detail records aliases on first view (Task 1.11's harvest), and search
  merges local alias hits in front of MusicBrainz results.

### Task 1.1: Reusable upstream rate limiter

**Objective:** One serialiser for MusicBrainz-class APIs (1 req/s, injectable clock).

**Files:**
- Create: `backend/app/services/rate_limit.py`
- Test: `backend/app/tests/test_rate_limit.py`

```python
class AsyncRateLimiter:
    """Space calls at least `min_interval` apart, sharing one lock.

    MusicBrainz allows 1 req/s sustained and answers 503 to bursts. Doing the
    spacing client-side is cheaper than discovering it in production.
    """

    def __init__(self, min_interval: float = 1.0, *, clock=time.monotonic,
                 sleep=asyncio.sleep):
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if now < self._next_at:
                await self._sleep(self._next_at - now)
                now = self._clock()
            self._next_at = now + self._min_interval
```

**Tests:** fake clock records sleeps; two concurrent `wait()` calls produce sleep ≈
`min_interval`; first call never sleeps.

**Run:** `pytest app/tests/test_rate_limit.py -q` → 3 passed.

**Commit:** `feat(catalog): add upstream rate limiter`

---

### Task 1.2: MusicBrainz client

**Objective:** Search / lookup / browse with a UA, retries and the limiter.

**Files:**
- Create: `backend/app/services/musicbrainz.py`
- Test: `backend/app/tests/test_musicbrainz.py`

Methods:
- `search_release_groups(query, limit=10, offset=0)` → `GET /release-group/?query=…&fmt=json`
  with `normalize_query()` applied (Task 1.11)
- `get_release_group(mbid)` → `GET /release-group/{mbid}?inc=artist-credits+releases&fmt=json`
- `get_artist(mbid)` → `GET /artist/{mbid}?inc=url-rels&fmt=json`
- `browse_artist_release_groups(artist_mbid, type_="album", limit=100, offset=0)` →
  `GET /release-group?artist=…&type=…&inc=artist-credits&fmt=json`

Header: `User-Agent: localsync-ai/<version> ( https://github.com/szehui/localsync-ai )`.
Retry `{429, 502, 503, 504}` with `(1.0, 3.0, 6.0)` + jitter — copy the shape from
`services/lidarr.py`. Raise `MusicBrainzUnavailable` after the final attempt so callers
can answer 503 instead of 500.

**Test:** replay a scripted 503-then-200 (as `test_lidarr_retry.py` does); assert the UA
header is present on every request; assert the limiter was awaited once per call.

**Commit:** `feat(catalog): add MusicBrainz client`

---

### Task 1.3: Result hygiene filters

**Objective:** Never surface junk as a requestable album.

**Files:** Modify `backend/app/services/musicbrainz.py`; test in `test_musicbrainz.py`

```python
# Audiobooks and interviews are release groups too; nobody wants them queued.
JUNK_SECONDARY = {"interview", "audiobook", "spokenword", "audio drama", "demo",
                  "dj-mix", "mixtape/street"}
REQUESTABLE_PRIMARY = {"album", "ep"}

def is_requestable(rg: dict) -> bool:
    primary = (rg.get("primary-type") or "").lower()
    if primary not in REQUESTABLE_PRIMARY:
        return False
    secondary = {s.lower() for s in (rg.get("secondary-types") or [])}
    return not (secondary & JUNK_SECONDARY)
```

`search_release_groups(..., requestable_only=True)` filters *after* scoring, keeps score
order, and reports `filtered_out` in the response so the UI can say "N results hidden".

**Tests:** Compilation and Live survive; Interview and Audiobook do not; Single is
excluded from default search but allowed when `requestable_only=False`.

**Commit:** `feat(catalog): filter non-requestable release groups`

---

### Task 1.4: Catalog cache table

**Objective:** Instant repeat searches and cheap rails without blowing the 1 req/s budget.

**Files:**
- Modify: `backend/app/models/database.py` (model + `init_db()`)
- Create: `backend/app/services/catalog_cache.py`
- Test: `backend/app/tests/test_catalog_cache.py`

```python
class CatalogCache(Base):
    __tablename__ = "catalog_cache"
    key = Column(String, primary_key=True)          # e.g. "search:daft punk:0"
    kind = Column(String, nullable=False, index=True)  # search | album | artist | rails
    payload = Column(Text, nullable=False)          # JSON
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

TTLs (seconds): search 600, album 604800, artist 86400, rails 3600.
API: `get(db, key, kind) -> dict | None` (deletes and returns None when stale),
`put(db, key, kind, payload)`.

**Migration:** `CREATE TABLE IF NOT EXISTS catalog_cache (...)` inside `init_db()` —
`create_all()` covers fresh DBs, the explicit block covers deployed ones.

**Tests:** fresh hit, stale miss deletes the row, payload round-trips.

**Commit:** `feat(catalog): cache upstream catalog responses`

---

### Task 1.5: Cover Art Archive client

**Objective:** Artwork by release-group MBID, tolerant of the many 404s.

**Files:** Create `backend/app/services/coverart.py`; test `test_coverart.py`

`front_cover_url(mbid, size="500")` → `GET https://coverartarchive.org/release-group/{mbid}`
and return `images[front].thumbnails[size]` or `image`. Return `None` on 404 — do not
raise, do not retry. Cache results (including negative results) through
`catalog_cache` with kind `coverart`, TTL 7 days.

**Test:** 200 with front image, 200 with no front image, 404 → None and cached.

**Commit:** `feat(catalog): add Cover Art Archive client`

---

### Task 1.6: ListenBrainz rails

**Objective:** New releases and "most played" without new credentials.

**Files:** Create `backend/app/services/lb_rails.py`; test `test_lb_rails.py`

- `fresh_releases(days=14)` → `GET /1/explore/fresh-releases/?days=N` → keep rows with
  `release_group_mbid`, dedupe by RG, map to the catalog card shape. The payload is ~2 MB
  for 14 days; cache it for an hour and never call it per page view.
- `popularity(release_group_mbids)` → `POST /1/popularity/release-group` with
  `{"release_group_mbids": [...], "count": N}` (max 100 ids per call; chunks of 25).
- `artist_popularity(artist_mbids)` → same for `/1/popularity/artist`.

**Tests:** POST body shape asserted (this is the 405/400 trap); chunking at 25; empty
list returns `{}` without a call.

**Commit:** `feat(catalog): add ListenBrainz rails`

---

### Task 1.7: Catalog service (merge + ownership)

**Objective:** One response shape for search/detail/rails, annotated per caller.

**Files:** Create `backend/app/services/catalog.py`; test `test_catalog.py`

Response card:

```python
{
  "release_group_mbid": str, "title": str, "artist_name": str, "artist_mbid": str,
  "year": int | None, "primary_type": str, "secondary_types": [str],
  "cover_url": str | None, "score": int | None,
  "owned": bool, "owned_via": "mbid" | "name" | None,
  "request": {"id": int, "status": str, "requested_by": str} | None,
}
```

Ownership lookup:
1. `albums.release_group_mbid == rgid` → owned, `owned_via="mbid"` (any
   `mbid_source`; tags and the Lidarr oracle are equally authoritative once validated).
2. folded `artist|album` match in `albums` → owned, `owned_via="name"` (shown with a
   "probably" tooltip; this is the untagged floor — 716 single-track rips and 135
   `[Unknown Album]` tags that no MBID backfill will ever reach).
3. `requests` open row for the RGID → attach status + requester display name.

Name matching here needs no MBID, which is why the untagged floor is a dedupe
inconvenience rather than a correctness hole: a user requesting an album they own as an
untagged rip still gets "probably in your library" instead of a silent duplicate.

`search(db, user, query)` = MB search → filter → CAA batch (top 10 only) → annotate.
`artist_detail(db, user, artist_mbid)` = MB artist + browse album/ep + annotate +
per-album requested/owned, plus a discography summary (`owned`, `requestable`).

**Tests:** ownership precedence (MBID beats name), a request in `denied` does not block a
new card, cover fetch only for the visible page.

**Commit:** `feat(catalog): merge catalog sources with ownership`

---

### Task 1.8: Catalog router

**Files:** Create `backend/app/routers/catalog.py`; modify `backend/app/main.py`;
test `test_catalog_endpoints.py`

- `GET /api/catalog/search?q=&limit=10&offset=0`
- `GET /api/catalog/album/{mbid}`
- `GET /api/catalog/artist/{mbid}`
- `GET /api/catalog/rails` → `{new_releases: [...], trending: [...]}`

All require `get_current_user`. Empty `q` → 400 with a message, not an upstream call.
`MusicBrainzUnavailable` → 503 `{"detail": "MusicBrainz is busy; retry in a few seconds"}`.

**Tests:** 401 without token; 400 on empty q; 503 mapping; happy path with a fake MB
client.

**Commit:** `feat(catalog): expose catalog API`

---

### Task 1.9: Navidrome native client + MBID sync pass

**Objective:** Fill `albums.release_group_mbid` from the only API that has it.

**Files:**
- Create: `backend/app/services/navidrome_native.py`
- Modify: `backend/app/models/database.py`, `backend/app/services/sync.py`
- Test: `test_navidrome_native.py`, extend `test_sync.py`

Client: `POST {url}/auth/login {"username","password"}` → token; header
`x-nd-authorization: Bearer <token>`; `iter_albums(page_size=200)` yielding rows until a
short page. Token cached in-process for 12 h.

Sync pass `sync_album_mbids(db)`: page native albums, `UPDATE albums SET
release_group_mbid=?, mbz_album_id=? WHERE id=?` for rows present; count matches,
misses, and `release_group_mbid IS NULL`. Run it at the end of `full_sync()`, using the
owner's credentials (`users.is_owner=True`, else lowest id). Never fail the whole sync
because the native API is down — log and continue.

**Tests:** pagination (2 pages), 401 → single re-login then error, update by album id,
duplicate/absent ids tolerated.

**Commit:** `feat(sync): capture release-group MBIDs from Navidrome`

---

### Task 1.10: Three-tier MBID resolution (tags → Lidarr oracle → MB tail)

Measured on the real library (research doc, "MBID coverage" appendix): Navidrome tags
cover 36.8 %, the Lidarr oracle adds 33.4 points for zero API calls, and a MusicBrainz
tail pass is worth another ~5. Do them in that order.

**Files:**
- Create: `backend/app/services/mbid_backfill.py`
- Modify: `backend/app/models/database.py` (columns below), `services/sync.py`
- Test: `test_mbid_backfill.py`

**Schema:**

```python
release_group_mbid = Column(String, nullable=True, index=True)
mbz_album_id = Column(String, nullable=True)
mbid_source = Column(String, nullable=True)      # navidrome_tag|lidarr_oracle|musicbrainz_backfill
mbid_confidence = Column(String, nullable=True)  # high|medium|low
mbid_checked_at = Column(DateTime, nullable=True)  # NULL = never attempted
```

`mbid_checked_at` is what makes the job resumable and is the only way to distinguish
"never looked" from "looked, found nothing" — without it every run re-queries the tail.

**Tier 1 — tags.** Task 1.9. 1,334 / 3,626.

**Tier 2 — Lidarr oracle.** Every Lidarr album carries `foreignAlbumId`, the release
group Lidarr itself resolved. Fold `artist|title` on both sides and copy the id where
exactly one RG maps to that key; skip keys with more than one candidate. Measured:
**1,210 of the 1,524 matchable untagged albums (79.4 %)**, zero MusicBrainz calls.
Validation on 20 sampled copies: 18 exact, 1 edition variant, 1 artist-credit variant
(`Pharrell` vs `Pharrell Williams`) — **zero wrong release groups.**

**Tier 3 — MusicBrainz tail.** 314 albums across 177 artists. Search the artist once,
browse `release-group?artist=<mbid>&type=album|ep` (one type per call), then score
locally. ~400 calls at 1 req/s, resumable, 10–15 minutes.

**Scoring — title+artist is identity, year and track count are disambiguators:**

- Normalise: strip disc/act splits (`(CD 1)`, `- Act 1`), trailing edition phrases
  ("25th Anniversary Edition"), and leading articles (`The Joshua Tree`).
- Accept on normalised title + artist equality. Use year (`date`/`maxYear` — Navidrome's
  album object has **no `year` field**) and track count only to choose between
  *multiple* same-title candidates.
- **Never let year or track count veto a unique match.** Measured counter-examples: Run
  DMC's *Raising Hell* has local `maxYear` 1999 against RG 1986 (a reissue); Notorious
  B.I.G.'s *Doing It Real Big* has 4 local tracks against 14 (partial rip). Strict gating
  rejected both correct matches and cut the tail hit rate from ~60 % to 33 %.
- Ambiguity guard as in the oracle: more than one strong candidate → store nothing.

**Acceptance:** after tiers 1–2, `release_group_mbid` non-null on ≥70 % of albums with no
MusicBrainz calls; tier 3 raises it into the mid-70s and stores nothing below `high`
without a confidence label. `/api/library/albums` exposes `release_group_mbid`,
`mbid_source`, `mbid_confidence`.

**Commit:** `feat(sync): three-tier MBID backfill (tags, Lidarr oracle, MB tail)`

---

### Task 1.10b: Report the floor honestly

The API-only ceiling is ~75–78 %. The remaining ~800 albums are 716 single-track rips,
135 `[Unknown Album]` tags, and ~15 mojibake titles; they need file-level fixes
(`beet mbsync`, AcoustID) and are out of scope. Log them as `mbid_checked_at` set with a
NULL mbid so they are never re-queried, and rely on the name-match tier — which needs no
MBID — to catch them for availability.

---

### Task 1.11: Non-Latin titles (the K-pop problem)

**Objective:** A user who knows "PSY 9th" or "BTS The Most Beautiful Moment in Life"
finds `싸다9` / `화양연화 pt.1` — and can tell which one they are looking at.

Phase 0 proved this is a presentation and query-shaping problem, not a catalog gap: the
`싸다9` release group literally contains a release titled `PSY 9th`, and MB's
release-group search indexes release titles.

**Files:**
- Modify: `backend/app/services/musicbrainz.py` (`normalize_query`, `also_known_as`)
- Modify: `backend/app/services/catalog.py`, `backend/app/models/database.py`
- Test: `test_musicbrainz_query.py`, `test_catalog_aliases.py`

**Step 1: query normalization**

```python
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

def normalize_query(text: str) -> str:
    """Shape a user query for MB's Lucene parser.

    `releasegroup:"화양연화 pt.1"` returns 503: punctuation inside a quoted
    field trips the analyzer. Dropping punctuation and scoping the field
    returns all three 화양연화 release groups instead.
    """
    text = " ".join(text.split())
    if CJK.search(text):
        stripped = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
        return f"releasegroup:({stripped.strip()})"
    return text
```

Keep `score` ordering; do not re-rank Latin results (Phase 0: `PSY 9th` puts the target
at rank 2, ahead of a cover act).

**Step 2: `also_known_as`**

On album detail (`get_release_group(mbid, inc="artist-credits+releases")`), collect the
distinct `release.title` values that differ from the release-group title after folding.
Return them as `also_known_as: [str]`. For `싸다9` that yields `["PSY 9th"]`.

Persist in a new table (aliases are a property of the release group, not the user):

```python
class AlbumAlias(Base):
    __tablename__ = "album_aliases"
    release_group_mbid = Column(String, primary_key=True)
    alias = Column(String, primary_key=True)
    source = Column(String, nullable=False, default="release_title")
    created_at = Column(DateTime, default=datetime.utcnow)
```

**Step 3: local matching**

`catalog.search` first checks `album_aliases` for a folded substring match and merges
those hits above upstream results (they are already owned/known MBIDs, zero API cost).
This is what makes a later typeahead for "psy 9th" instant.

**Step 4: artist-first fallback**

When an album search yields no requestable rows, search `/artist?query=` and return
`artist_suggestions: [{artist_mbid, name, disambiguation, score}]` so the UI can say
"no albums matched — see PSY's discography". Artist results must be ranked by score and
disambiguation shown: MB contains several artists named ENHYPEN and `results[0]` picked
one with 3 albums.

**Tests:** Hangul query gets fielded + punctuation-stripped; `also_known_as` excludes
the identical title and dedupes; alias match surfaces a card without an MB call; no-hit
search returns artist suggestions; Latin query is passed through unchanged.

**Commit:** `feat(catalog): non-Latin titles and alias fallback`

---

# Phase 2 — Requests

> **Status: shipped 2026-11-28.** All eight tasks complete, 839 tests passing,
> deployed, and verified live against real MusicBrainz + Lidarr. See DEVLOG.
> Deviations from the plan as written:
> - Task 2.1 also folded in the per-user `discovery_state` rebuild (deferred from
>   the 11-27 entry) — a singleton row was showing one user's status to another.
> - `fetch_release_group` translates MusicBrainz **400** to `404 not_found`; a
>   well-formed nonexistent UUID is a 400, not a 404, and it was 500ing live.
> - The partial index is named `ix_requests_open_rg` (created with the model in
>   Phase 1), not `uq_requests_open_rg`. Same predicate, one index, no duplicate.
> - `add_albums_by_mbid` verifies monitoring against `GET /album` and reports
>   `monitoring_ok`; Lidarr's async `RefreshArtist` can re-create rows unmonitored.

### Task 2.1: Models and migrations

**Files:** Modify `backend/app/models/database.py`; test `test_requests_models.py`

```python
class Request(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    kind = Column(String, nullable=False, default="album")      # album | artist
    release_group_mbid = Column(String, nullable=True, index=True)
    artist_mbid = Column(String, nullable=True, index=True)
    artist_name = Column(String, nullable=True)
    album_title = Column(String, nullable=True)
    year = Column(Integer, nullable=True)
    primary_type = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    scope = Column(String, nullable=True)              # artist requests: studio|studio_eps|everything
    parent_request_id = Column(Integer, ForeignKey("requests.id", ondelete="CASCADE"),
                               nullable=True, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    note = Column(Text, nullable=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    lidarr_album_id = Column(Integer, nullable=True)
    lidarr_artist_id = Column(Integer, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    available_evidence = Column(String, nullable=True)  # mbid | name
```

`RequestVote(id, request_id FK CASCADE, user_id FK CASCADE, created_at,
UniqueConstraint(request_id, user_id))`.

`Blocklist(id, release_group_mbid, artist_mbid, scope, reason, created_by, created_at)`.

`users.auto_approve = Column(Boolean, default=False)` and
`users.can_request = Column(Boolean, default=True)`.

Partial unique index so one album cannot have two open requests but a denied one can be
re-requested:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_open_rg
  ON requests(release_group_mbid)
  WHERE kind = 'album' AND status NOT IN ('denied', 'cancelled');
```

`init_db()` must also `ALTER TABLE users ADD COLUMN auto_approve BOOLEAN DEFAULT 0` and
`can_request BOOLEAN DEFAULT 1`, plus the index above.

**Tests:** fresh schema builds; `init_db()` against the pre-request schema adds columns
and the index; partial index allows denied → new pending.

**Commit:** `feat(requests): add request, vote and blocklist models`

---

### Task 2.2: Request service — create, dedupe, vote

**Files:** Create `backend/app/services/requests.py`; test `test_requests_service.py`

`create_album_request(db, user, rgid, note=None)`:
1. Reject if `rgid` is blocklisted (owner scope) → `RequestError("blocked")`.
2. Fetch the RG from MB (cache first). Reject if `not is_requestable` → `RequestError`.
3. If an open request exists for the RGID: add/refresh the caller's vote and return
   `{"request": existing, "created": False, "voted": True}`. Do not POST twice.
4. Insert `pending` (or `approved` when `user.auto_approve`), then call `push_to_lidarr`
   when auto-approved.
5. Cache the display fields (title, artist, year, type, cover) on the row so the request
   list does not need upstream calls.

**Tests:** duplicate request becomes a vote; second vote is idempotent; blocked RG
refused; auto-approve flips status and calls the pusher; status starts `pending` when the
flag is off.

**Commit:** `feat(requests): create/ dedupe/ vote album requests`

---

### Task 2.3: Lidarr MBID resolve + batch add

**Objective:** Add exactly the requested release groups, collapsing once per artist.

**Files:** Modify `backend/app/services/lidarr.py`; test `test_lidarr_mbid.py`

```python
async def lookup_album_by_mbid(self, mbid: str) -> Optional[Dict[str, Any]]:
    """Exact release-group lookup. Returns None rather than a best guess."""
    rows = await self.lookup_album(f"mbid:{mbid}")
    for row in rows or []:
        if (row.get("foreignAlbumId") or "").lower() == mbid.lower():
            return row
    return None
```

Refactor `_add_album_core` into:
- `_resolve_album(artist_name, release_name)` — the existing title path.
- `_resolve_album_by_mbid(mbid)` — the new path.
- `_post_album(album, defaults, search)` — payload building + POST + `already_present`
  short-circuit + optional search. Both resolvers feed this, so there is one add path.

`add_albums_by_mbid(mbids, search=False, delay=1.0)`:
- resolve each MBID (skip unresolved, report them, never substitute);
- POST each album;
- collect `{artist_id: [album_ids]}` across the whole batch;
- one `_monitor_only(artist_id, album_ids)` per artist at the end;
- verify by re-reading `artist_monitored_albums` and return `monitoring_ok`.

**Tests:** lookup returns a mismatched `foreignAlbumId` → None (no add); two albums by one
artist produce **one** `PUT album/monitor` with both ids; unresolved MBID is reported and
not replaced by another result.

**Commit:** `feat(lidarr): add albums by release-group MBID`

---

### Task 2.4: Approve / deny / cancel / retry

**Files:** Modify `backend/app/services/requests.py`; test `test_requests_lifecycle.py`

- `approve(db, request, actor)` — owner-only unless `request.user_id == actor.id` and
  `actor.auto_approve`; sets status `approved`, pushes to Lidarr, stores
  `lidarr_album_id`/`lidarr_artist_id`, sets `last_error=None`.
- `deny(db, request, actor, reason)` — owner-only; status `denied`, `decided_*` filled.
- `cancel(db, request, actor)` — requester or owner, only while `pending`.
- `retry(db, request, actor)` — owner-only; re-resolve + re-add; keeps the same row.
- Push failures: status stays `approved` with `last_error` populated and the request is
  retryable. Never mark `failed` for a transport error.

**Tests:** approval permission matrix; transient Lidarr 503 keeps `approved` +
`last_error`; retry after a fixed upstream succeeds and clears the error.

**Commit:** `feat(requests): approval lifecycle`

---

### Task 2.5: Permissions

**Files:** Modify `backend/app/routers/auth.py`, `backend/app/services/users.py`;
test `test_request_permissions.py`

Add `require_owner(user)` (reuse the existing pattern at `auth.py:279`) and a
`can_request` check on creation. Owner-only endpoints: approve, deny, retry, blocklist,
toggling another user's `auto_approve`/`can_request`. Extend `GET /api/auth/users` with
both flags and a `PATCH /api/auth/users/{id}` owner-only toggle.

**Tests:** non-owner cannot approve another user's request (403), owner can; a user with
`can_request=False` gets 403 on create; users cannot toggle flags.

**Commit:** `feat(auth): request permissions and auto-approve flag`

---

### Task 2.6: Artist discography requests

**Files:** Modify `backend/app/services/requests.py`; test `test_artist_requests.py`

`create_artist_request(db, user, artist_mbid, scope="studio")`:
1. Browse MB for `album` (and `ep` when scope is `studio_eps`/`everything`). **One type
   per call** — repeating `type` returns 503 (Phase 0).
2. Filter with `is_requestable`; for `studio` also drop secondary types entirely.
   Soundtracks and compilations (`['Compilation', 'Soundtrack']` on the Shang-Chi album)
   therefore fall outside a studio request by design — the UI must say "17 studio
   albums (soundtracks and compilations excluded)", and direct search can still request
   them individually.
3. Drop owned albums (by RG MBID) and open requests; the rest become child album
   requests with `parent_request_id`.
4. Parent row: `kind="artist"`, `status="pending"`, plus a rollup computed on read:
   `{requested, pending, approved, downloading, available, failed}`.
5. Cap at `MAX_ARTIST_ALBUMS = 100`; refuse above it. Return the count so the UI can
   confirm before creating ("Request 17 albums by Daft Punk?").

On approval, the parent approves all children and calls `add_albums_by_mbid` **once** so
monitoring collapses once for the whole discography.

**Tests:** expansion excludes owned + already-requested; EP filter; rollup counts; cap
refusal; batch add invoked once with 17 ids; one collapse per artist.

**Commit:** `feat(requests): artist discography requests`

---

### Task 2.7: Requests router

**Files:** Create `backend/app/routers/requests.py`; modify `main.py`;
test `test_requests_endpoints.py`

- `POST /api/requests` `{kind, release_group_mbid | artist_mbid, scope?, note?}`
- `GET /api/requests?scope=mine|all&status=` (all = owner only)
- `POST /api/requests/{id}/approve|deny|cancel|retry`
- `POST /api/requests/{id}/vote`
- `PATCH /api/requests/{id}` (owner note)

Responses include display fields so the UI needs no catalog calls to render a list.
Cross-user access returns **404**, matching the existing trigger convention.

**Commit:** `feat(requests): expose requests API`

---

### Task 2.8: Wire discovery → requests

**Files:** Modify `backend/app/routers/discovery.py`, `frontend` later

`POST /api/discovery/{offer_id}/add` stops calling `LidarrService.add_single_album`
directly. It resolves the offer's release group (it already has `release_name` +
`artist_name`; resolve to an MBID via the existing `release_mbid` when present, else a
search that requires artist agreement) and creates a **request**. Offers without a
resolvable MBID stay `offered` and are reported, not silently added.

**Tests:** resolvable offer → one request + status `saved`; unresolvable → 422 with a
message and no Lidarr call.

**Commit:** `refactor(discovery): route additions through requests`

---

# Phase 3 — Status truth

> **Status: shipped 2026-11-28.** All three tasks complete, 909 tests passing,
> deployed, verified live against real Lidarr (4,753 albums, 42-item queue) and
> the real library (2,069 tagged albums). Deviations from the plan as written:
> - **A vanished album is not auto-re-added.** The plan called for one re-add
>   then `failed`; instead the reconciler marks `failed` with "no longer in
>   Lidarr — retry to add it again" and leaves re-adding to the owner's retry
>   action. Silently resurrecting an album somebody deleted in Lidarr is worse
>   than reporting it.
> - **`failed` rows stay watched for 30 days** (when Lidarr still has the album).
>   Lidarr's own RSS/auto-search can deliver after our attempt failed, and a
>   request that quietly became available should say so. The plan only
>   reconciled "open" requests, which would have left those rows frozen.
> - **`albumImportIncomplete` is not treated as a failure.** It fires on every
>   partial import — it was 3 of the 5 most recent history events on the live
>   instance — and album statistics describe that case more precisely.
> - **`catalog.OPEN_STATUSES` was wrong and is now derived.** It read
>   `("pending", "approved", "processing")`: `processing` is not a status in the
>   machine, and `downloading`/`imported` were missing, so a card for an album
>   already downloading offered "Request" again.
> - **Track-count agreement is only enforced when a count is known.** `requests`
>   has no `song_count` column, so with no Lidarr row there is nothing to compare
>   and the folded-name path stays permissive — the same call the cards make.

### Task 3.1: Reconciler

**Files:** Create `backend/app/services/request_sync.py`; test `test_request_sync.py`

Per cycle:
1. `GET album?foreignAlbumId=<mbid>` for each open album request (one call each; Lidarr is
   local and this filter is verified working).
2. `GET queue?page=1&pageSize=200` **once**, index by `albumId`.
3. `GET history?page=1&pageSize=100&sortKey=date&sortDirection=descending` **once**,
   index failures by `albumId` with their date.
4. Transition:

| Condition | Status |
| --- | --- |
| Navidrome has the RG MBID (or name+artist+songCount match) | `available` (+ `available_evidence`) |
| `statistics.percentOfTracks == 100` but not in Navidrome yet | `imported` ("finishing up") |
| `0 < percentOfTracks < 100` | `partially_available` |
| album id in queue | `downloading` |
| history has `downloadFailed`/`importFailed` after `requested_at` | `failed` + reason |
| album row vanished and re-add fails | `failed` ("released from Lidarr") |
| `lastSearchTime` older than 48 h, no files | `failed` ("no release found") |
| otherwise | `approved` |

**An unmonitored album row is not evidence of an add.** `POST /album` creates the artist
*and* its discography metadata — Phase 0's RAM add left three unmonitored Daft Punk
albums behind. Only the album id captured at add time, re-resolved by `foreignAlbumId`
and confirmed `monitored`, counts as the request being in Lidarr.

Never move backwards (`available` never returns to `downloading`). `last_synced_at` on
every touched row.

**Tests:** one fixture per row of the table; no-backwards guard; vanished album attempts
one re-add then fails.

**Commit:** `feat(requests): reconcile status with Lidarr and Navidrome`

---

### Task 3.2: Scheduler + manual refresh

**Files:** Modify `backend/app/services/scheduler.py`, `main.py`, `routers/requests.py`

- APScheduler job every 5 minutes: `reconcile_open_requests()` in its own DB session.
  Skip when there are zero open requests (no Lidarr calls on an idle household).
- `POST /api/requests/sync` (owner) triggers it immediately and returns counts.
- Log a one-line summary: `requests synced: n, changed: m, failed: k`.

**Tests:** zero-open short-circuits; session is created/closed; manual endpoint
owner-only.

**Commit:** `feat(requests): schedule status reconciliation`

---

### Task 3.3: Availability helper (single source)

**Files:** Modify `backend/app/services/catalog.py`; test `test_availability.py`

`library_has_release_group(db, rgid, artist, title, song_count)`:
1. MBID match → `"mbid"`.
2. Folded `artist|title` match where `abs(album.song_count - song_count) <= 1` → `"name"`.
3. Otherwise `None`.

Both the catalog cards and the reconciler call this — one definition, so the badge in
search and the status in the request list can never disagree.

**Tests:** MBID wins; name match needs track-count agreement; a same-title different
artist does not match.

**Commit:** `refactor(catalog): single availability predicate`

---

# Phase 4 — Frontend

> **Status: shipped 2026-11-28.** All tasks complete, 922 tests passing.
> Files landed as `catalog.tsx` (shared card/pill/cover), `views/CatalogHome.tsx`,
> `views/CatalogSearch.tsx`, `views/AlbumModal.tsx`, `views/ArtistView.tsx`,
> `views/RequestsView.tsx` — not the `SearchView`/`AlbumDetail` names sketched
> below, because home rails and the artist page were added to the phase.
>
> **Deviations, all recorded in DEVLOG:**
> - Task 4.2 says the badge is exactly `In library` / `Requested by` / `Request`.
>   Implemented as written, plus a fourth case: `Probably in library`, for a
>   library copy matched by artist+name with no MBID tag. Calling that "In
>   library" would overstate the evidence, and the Phase 0 ownership index
>   already distinguishes it.
> - Task 4.3 asks for a **track count** on the album modal. MusicBrainz release
>   groups do not carry one without a separate release lookup, so the modal shows
>   **edition count** instead ("2 editions"). Showing a number we did not fetch
>   would be worse than showing the one we did.
> - Task 4.6 (rails) was scheduled to be "new releases + trending". It shipped,
>   but only after three fixes: ListenBrainz popularity parsed as a dict instead
>   of a list (every call 500ed), rail titles read keys the feed does not have
>   (every card blank), and 3,597 cards rendered in 37 s.
> - The API needed one addition the plan did not anticipate: `/auth/me` now
>   returns `is_owner`, `auto_approve`, and `can_request`. Without them the UI
>   cannot decide whether to offer Approve except by provoking a 403.
> - The compose network is now dual-stack. That is not a UI change, but the
>   frontend is what made it visible: MusicBrainz is IPv6-first and Docker's
>   default bridge is IPv4-only.

### Task 4.1: Types + API client

**Files:** Modify `frontend/src/types.ts`, `frontend/src/api.ts`

Add `CatalogAlbum`, `RequestItem`, `ArtistDetail`, `Rails`. `api.ts` gets
`searchCatalog(q, limit)`, `getAlbum(mbid)`, `getArtist(mbid)`, `getRails()`,
`createRequest`, `listRequests`, `approveRequest`, `denyRequest`, `cancelRequest`,
`retryRequest`, `voteRequest`. Every call goes through the existing auth header helper.

**Commit:** `feat(ui): catalog and request API client`

---

### Task 4.2: Search view

**Files:** Create `frontend/src/views/SearchView.tsx`; test via a component smoke test if
the repo has one, otherwise manual verification steps

- 400 ms debounce, `AbortController` cancels in-flight requests.
- States: idle → typing → loading → results / empty ("No matches; try artist + album") /
  error ("MusicBrainz is busy — retry in a few seconds").
- Result row: cover, title, artist, year, primary type, edition count, and a badge that
  is exactly one of `In library` / `Requested by <name>` / `Request`.
- Never render a bare "Request" button for a blocklisted or non-requestable RG.

**Commit:** `feat(ui): catalog search view`

---

### Task 4.3: Album detail + request action

**Files:** Create `frontend/src/views/AlbumDetail.tsx` (or a modal component)

Shows cover, artist (links to artist view), year, type/secondary types, track count,
edition count, and — when not owned — a Request button with an optional note field.
On success, the button becomes `Pending approval` or `Approved — downloading`, matching
the server's status, with no optimistic invention.

**Commit:** `feat(ui): album detail and request action`

---

### Task 4.4: Requests view

**Files:** Create `frontend/src/views/RequestsView.tsx`

Two modes by permission: **Mine** (default) and **All** (owner). Rows show cover, title,
artist, requester, status pill, age, and a vote count. Owner rows get approve/deny/retry
actions with a reason field for deny. Status pills map 1:1 to the server enum — no
client-side inference.

**Commit:** `feat(ui): requests view`

---

### Task 4.5: Artist view + discography request

**Files:** Create `frontend/src/views/ArtistView.tsx`

Album grid with per-album state (`In library` / `Requested` / `Request`). A
"Request discography" panel lets the user pick scope (`studio` / `studio_eps` /
`everything`) and shows the exact count before confirming ("17 albums; 3 already in your
library"). Above 15 albums the confirm requires an explicit click on a second button.

**Commit:** `feat(ui): artist view and discography requests`

---

### Task 4.6: Landing rails

**Files:** Create `frontend/src/views/DiscoverHome.tsx`; modify `App.tsx`

Two horizontal rails — **New this week** (LB fresh releases) and **Most played right now**
(LB popularity for artists already in the library, which makes it feel personal without a
new credential). Each card requests or opens the album. Rails load from
`GET /api/catalog/rails` (cached server-side for an hour) and degrade to hidden on error.

**Commit:** `feat(ui): new-release and trending rails`

---

### Task 4.7: Navigation

**Files:** Modify `frontend/src/App.tsx`

TABS become: `Home` (rails), `Search`, `Requests`, `Discover`, `Playlists`, `Automation`,
`YouTube`, `Source`. Owner sees the same `Requests` tab with an "All" toggle rather than a
separate admin tab. Keep the existing tab styling and `description` pattern.

**Commit:** `feat(ui): wire music request navigation`

---

# Phase 5 — Polish

> **Status: 5.2, 5.3, 5.4 shipped 2026-11-28 (as one commit). 5.1 deferred.**

### Task 5.1: Availability badge on library views — DEFERRED

Show `release_group_mbid` presence (or absence) in album lists so the household can see
which albums are untagged — the same slice that will forever be "probably owned".

**Why deferred:** the badge would only make the 499-album Subsonic gap visible in a
second place. The gap's root cause (why Navidrome reports albums Subsonic does not) is
the larger, still-unexplained hole in ownership reporting; a badge is worth building
once that is understood, so it can say *why* an album is unmatched rather than just that
it is.

**Commit:** `feat(ui): show tagging/MBID state`

---

### Task 5.2: Discord webhook notifications — SHIPPED

**Files:** `backend/app/services/notifier.py`, `backend/app/routers/notifications.py`,
model `NotificationConfig`, column `requests.notified_status`.

Events: `request.created`, `request.available`, `request.failed`. Fire-and-forget with a
5 s timeout; a failed webhook never fails the request transition. Config is a single row
`{webhook_url, base_url, enabled, events}`, owner-only UI under **Manage**.

**Deviations from the plan, all deliberate:**

- **Destination is one webhook for the household**, not one channel per role. A second
  webhook to route "owner" vs "requester" events is a config surface with no user yet;
  the embed carries `Asked by`, which is what the owner needs to triage.
- **Child albums of a discography do not announce.** The parent fires once when the last
  album lands. The plan's literal reading would send 30 messages for one request.
- **The webhook URL is write-only over the API.** The plan said "config is a single row";
  it did not say the row holds a credential. `GET` returns a mask, `PUT` without a URL
  keeps the stored one, logs carry the mask.
- **Announcements are collected during the reconcile cycle and sent after the commit**, not
  fired inline: the reconciler runs each row inside a savepoint, and an HTTP call there
  would hold a write lock for a network round trip.
- **`base_url` column added** beyond the plan's three fields: the reconciler has no HTTP
  request to borrow a hostname from, so links in background notifications need a
  configured origin.

**Commit:** `feat(notifications): Discord webhook for request events`

---

### Task 5.3: Blocklist — SHIPPED

**Files:** `backend/app/services/blocklist.py`, `backend/app/routers/blocklist.py`,
`Blocklist` gains `album_title` / `artist_name` (display names, so the owner can review
the list months later without an upstream lookup).

Owner-only, two scopes. Enforced at creation (already true in Task 2.2, now shared through
one module), and additionally: search and rails drop blocked cards; artist pages keep them
greyed and non-requestable; the discography plan skips them and reports `blocked` in its
summary.

**Deviation:** the plan said "filtered from catalog results". Filtering alone would have
left a discography showing 38 of 39 albums with no explanation, so blocked albums are
*marked and disabled* on the artist page while being dropped from search and rails, where
there is no surrounding list to make an absence legible.

**Commit:** `feat(requests): blocklist`

---

### Task 5.4: Docs + dev log — SHIPPED

- `DEVLOG.md`: Phase 5 entry — the parent-status bug, the once-per-status rule, the two
  UI bugs the live test found, and what was verified vs. only unit-tested.
- `~/wiki/domains/infrastructure/localsync-ai.md`: notifications + blocklist section.

**Commit:** `docs: music requests`

---

## Verification matrix (must all pass before calling it done)

| Behaviour | Proof |
| --- | --- |
| A request adds exactly the requested RG | Unit test asserts `lookup?term=mbid:<uuid>` + `foreignAlbumId` equality; spike 0.1 live |
| No fuzzy matching on the request path | Test asserts `lookup_album_for` / `_pick_album` are never called by `add_albums_by_mbid` |
| Discography request collapses once | Test asserts one `PUT album/monitor` for N albums by one artist |
| Second requester votes, does not duplicate | Service test + endpoint test |
| Non-owner cannot approve | 403 test |
| Transient Lidarr 503 is retryable, not failed | Reconciler/push test |
| Failed download surfaces | Fixture with `downloadFailed` history → `failed` + reason |
| 100%-in-Lidarr-but-not-scanned is honest | `imported`, not `available` |
| Untagged album still shows as owned | Name+track-count availability test |
| Korean-titled album findable by its English name | `PSY 9th` query test (Phase 0: MB rank 2) + alias-table match |
| Reconciler ignores unmonitored sibling rows | Fixture from the Phase 0 Daft Punk case |
| MusicBrainz outage degrades, does not 500 | 503 mapping test |
| Deployed DB upgrades in place | `test_migration_upgrade_path.py` extended with the new schema |
| Test count | `≥ 551` and rising; no skipped tests |

---

## Out of scope (deliberately)

- Spotify / Discogs / iTunes as catalog sources (no MBIDs or ToS problems).
- Per-request quality-profile selection (Lidarr defaults are already correct).
- Quotas (user chose per-user auto-approve instead).
- Track-level requests (album is the smallest requestable unit; the existing discovery
  feed already operates at track granularity upstream).
- Migrating the YouTube import off title matching (separate, low-risk follow-up now that
  `_post_album` is shared).

# Phase 6 — Discovery that renews (shipped 2026-11-28)

Not in the original plan: Phase 5 verification exposed that the discovery feed
had not added an offer in four days, and that its seeds never came from
ListenBrainz at all. Both were fixed together.

### Task 6.1: ListenBrainz history as a seed source — done

`library_seed_pool()` (play counts) and `listenbrainz_seeds()` (90 days of
history, one seed per artist, junk artists dropped) are merged by
`merge_seed_pools()` under a quota: half the seed budget is reserved for
history-only artists.

Measured live: the play-count pool reached 66 artists and returned Labs
neighbours for 5 of 12 sampled seeds; 89 artists appeared only in history and
returned neighbours for 9 of 12. Merging without a quota would have kept the old
bias, since the library pool alone can fill the budget.

### Task 6.2: Seed rotation — done

New `discovery_seed_uses` table. `order_for_rotation()` pushes seeds scanned in
the last 14 days to the back of their pool; `record_seed_uses()` writes only the
seeds the scan actually reached, not the full selected list (early stop would
otherwise mark unexpanded seeds as explored).

Ordering matters and was wrong in the first cut: rotation must run *before* the
quota merge. After the merge there is nothing left to reorder, so run two
re-expands run one's seeds and adds zero.

Verified: run 2 added 122 offers from entirely different seeds.

### Task 6.3: Owned-set from the server, not just the mirror — done

`triage_candidates()` takes `owned_release_groups` and drops a candidate whose
release group is owned even when its recording MBID is unknown. The set is the
union of `albums.release_group_mbid` and a live `NavidromeNative.list_albums()`
walk (mirror covered 1,318 of 2,656 album MBIDs). A failing live read degrades
to the mirror and logs.

### Task 6.4: Offer expiry — done

`expire_stale_offers()` retires `offered` rows unseen for 45 days. `dismissed`
and `saved` never expire. `discovery_state` gained `lb_seeds_used` and
`offers_expired`, both ALTERed on upgrade; the UI shows the history share.

### Deliberately not done

- Per-seed diversity cap. Measured the result instead: 15 distinct seeds explain
  the top 40 feed artists, with a maximum of 14 — no cap needed yet.
- Re-offering expired rows. An expiry is not a dismissal, but resurrection is
  how a discovery feed becomes a treadmill; revisit only if the feed starves.
