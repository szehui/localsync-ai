# LocalSync AI — Development Log

## 2026-06-04 — Project Kickoff

### Decisions Made
- **Platform:** Web-based UI (React + FastAPI), single Docker container
- **Port:** 4535 (to avoid conflicts with existing services on server)
- **Scope:** Navidrome-only for v1, single-user, Docker deployment
- **Strictness:** Slider (1-5), not a binary toggle
- **Playlist naming:** `Fresh Discoveries YYYY-MM-DD`, `More Like This: <seed track>`, `Weekend Mix` (user-named)
- **Auth:** Subsonic username/password (salt+token), configured via UI
- **Sync:** Full sync on startup, incremental hourly via `getAlbumList2?type=newest`
- **Scheduler:** APScheduler with SQLite job store for persistence
- **Repo:** szehui/localsync-ai (public)

### Architecture
- FastAPI backend + React/Vite frontend
- SQLite for metadata cache + scheduler job store
- Navidrome Subsonic API at 192.168.4.205:4533
- nginx serves React SPA, proxies /api to FastAPI

### Phases
- Phase 0: Project setup ✅
- Phase 1: Foundation ✅
- Phase 2: Seed-based playlist generation ✅
- Phase 3: Smart Triggers ✅
- Phase 4: Frontend ✅
- Phase 5: Polish & Deploy ✅

---

## Phase 4: Frontend

### What was done
- React/Vite/Tailwind SPA with 4 views:
  - **SourceDashboard**: Navidrome connection form, library stats
  - **SeedInterface**: track search, strictness slider (1-5), generate + push
  - **PlaylistView**: list generated playlists with status badges
  - **AutomationHub**: trigger CRUD, enable/disable, type-specific config
- Shared components: Card, Button, Badge, Input, Slider, Select, Spinner, EmptyState
- Typed API client (all backend endpoints)
- Build: 38 modules, 165KB JS bundle

### Issues encountered
- Stale compiled `.js` files picked up by Vite instead of `.tsx` sources
  - Fixed by removing stale files, adding `*.js` to frontend `.gitignore`
- `vite.config.js` CJS/ESM mismatch with `"type": "module"` in package.json
  - Fixed by using `.ts` extension (ESM) instead of `.js` (CJS)
- Import resolution: explicit `.tsx` extensions needed to avoid `.js` ambiguity

---

## Phase 5: Docker Deployment

### What was done
- `.dockerignore`: excludes node_modules, .venv, dist, tests, .git
- Fixed docker-compose volume mount: `/app/backend/data` (matches backend working directory)
- Verified `docker compose build` succeeds end-to-end:
  - Stage 1: node:20-alpine → npm ci + vite build
  - Stage 2: python:3.11-slim → pip install + nginx + backend + frontend dist
  - Port 4535 → container port 80 (nginx)

### Deployment
```bash
docker compose up -d
# Open http://localhost:4535
```

### Final stats
- 44 backend tests passing
- Frontend: 38 modules, 165KB JS bundle
- Docker image: multi-stage, ~300MB
- Repo: https://github.com/szehui/localsync-ai

---

## Phase 0: Project Setup

### What was done
- Created GitHub repo szehui/localsync-ai
- Scaffolded project structure (backend/, frontend/, docker-compose.yml, Dockerfile)
- Created this dev log

### Issues encountered
- Git push failed with HTTPS — fixed with `gh auth setup-git`

---

## Phase 1: Foundation

### What was done
- FastAPI app with lifespan (DB init, scheduler start, trigger loading)
- NavidromeClient: full Subsonic API wrapper (auth, artists, albums, similarity, playlists, search)
- SyncService: full sync (artists→albums→tracks) + incremental sync (newest + play counts)
- SQLite models: Artist, Album, Track, GeneratedPlaylist, SmartTrigger
- 4 API routers: auth, library, playlists, triggers
- Playlist generation: seed-based with strictness slider (1-5), push/update on Navidrome
- Docker multi-stage build (Node → Python + nginx)
- 29 tests passing

### Issues encountered
- httpx.Response mock needs `request=` param for `raise_for_status()` to work
- SQLAlchemy model comparison with `==` triggers `__eq__` on Column — use `is` instead
- MagicMock `.all()` returns new MagicMock, not the configured return_value — need explicit setup

---

## Phase 2: Seed-Based Playlist Generation

### What was done
- `POST /api/playlists/generate` — seed track + strictness + count → ordered track list
- `POST /api/playlists/push` — create or update-in-place on Navidrome
- Strictness filter: 1=none, 2=artist/album, 3=artist|genre, 4=genre, 5=artist+genre
- 13 strictness filter tests

### Issues encountered
- `_passes_strictness` returned `None` instead of `False` when genre was None — fixed with `bool()`

---

## Phase 3: Smart Triggers

### What was done
- Scheduler service (`app/services/scheduler.py`):
  - `run_recency_trigger`: daily at 9 AM, "Fresh Discoveries" from newest albums
  - `run_heavy_rotation_trigger`: every 6h, "More Like This" for tracks above play threshold
  - `run_scheduled_trigger`: cron-based, refreshes named playlist from frequent tracks
  - Job management: add/remove/pause/resume APScheduler jobs
- Triggers router wired to scheduler: CRUD operations manage APScheduler jobs
- main.py loads existing enabled triggers on startup
- 15 new tests (44 total passing)

### Issues encountered
- `NavidromeClient` has `close()` not `aclose()` — fixed scheduler service calls
- Patch tool mangled file structure on first rewrite attempt — used write_file instead

---

## 2026-11-18 — ListenBrainz as Primary Data Source

### Why
Last.fm is introducing API charges. Listening history and similarity had to move
to a free source, with Last.fm kept as a fallback rather than a dependency.

### Architecture
ListenBrainz is now primary for both history and similarity; Last.fm only runs
when ListenBrainz yields nothing usable.

- **`services/listenbrainz.py`** — read-only client. Rate limited at 1 req/sec
  and honors `X-RateLimit-*` (30/window). Three quirks handled explicitly:
  - `204 No Content` returns an **empty body, not JSON** — treated as `[]`
    without parsing (a naive `json.load` crashes here)
  - listens nest the MBID under `track_metadata.mbid_mapping`, falling back to
    `additional_info`
  - Labs `similar-recordings` takes **one** MBID per request; batching returns 400
- **`services/lb_cache.py`** — recommendations cached 24h, Labs similar 7d per
  recording MBID. Empty lists are cached deliberately: `[]` means the dataset has
  no neighbours, which is stable and stops a 20-seed run repaying 20 calls daily.
- **`services/resolver.py`** — maps ListenBrainz → Navidrome song IDs with **no
  API calls**, via a dual local index.

### The MBID coverage problem
Live measurement over 120 albums / 1,548 songs: **50.2%** of tracks carry
`musicBrainzId`, and tagging is *per album* — 59 albums fully tagged, 61 fully
untagged, **0 mixed**. An MBID index alone therefore leaves half the library
unreachable, in whole-album chunks.

Confirmed on the full production library after deploy: **15,473 of 27,945 tracks
(55.4%)** carry an MBID, and the per-album shape holds exactly — of 2,934 albums,
**1,321 fully tagged, 1,613 fully untagged, 0 mixed**.

Fix: `tracks.search_key` — a normalized `<artist>|<title>` build at sync time
(`build_search_key`, NFKD → ASCII → lowercase → strip non-alphanumerics).
Resolution tries MBID first, then text, **per entry**: an MBID ListenBrainz knows
whose library copy is an untagged rip still resolves through text. Unresolved
entries get a capped (10) `search3` fallback.

Measured hit rate on the live library (1,000-track samples):
- MBID known to ListenBrainz: **99.8% resolved**
- text-only (no MBID): **99.4% resolved**
- realistic mix: **99.8% resolved**

Verified end-to-end with real listens: 4 of 4 scrobbles resolved to Navidrome
track IDs — two via MBID, two via the text index (because ListenBrainz had no
MBID mapping for them). That split is precisely the case the dual resolver exists
for.

Verified live on the production DB: **21,999/21,999** tracks have a `search_key`
after backfill.

### Blend refactor
The recency trigger's inline 60/40 selection became `_blend_tracks()` — pure,
seedable, tested for short pools, duplicates, and thin history. History side is
60% ListenBrainz / 40% Navidrome similarity; Last.fm only fills gaps. Heavy
rotation got the same treatment per seed, sourcing LB similarity via each track's
recorded MBID.

### Bootstrap mode
The account had **0 listens** at migration time (scrobbling was just enabled in
Navidrome), so ListenBrainz is a data desert until history accrues. The newest
pool target is now driven by how much history actually resolved: below 30 history
tracks it gathers up to 50 newest so a "50-track" mix is not silently 23 tracks.
`MIN_LB_LISTENS = 10` marks the thin-history threshold in logs.

### Bugs found while wiring this up
- `similar_from_nav` lived *inside* the Last.fm block — a successful ListenBrainz
  run silently lost Navidrome similarity entirely
- Heavy rotation indexed `search3.view`'s response envelope as a list
  (`nav_hits[0]["id"]`), raising `KeyError` that the surrounding `except`
  swallowed — Last.fm-sourced similarity had never contributed anything there
- With no history at all, the newest gather capped at 20, shipping a 20-track mix
- **Stale library rows (found on the production DB).** 20,880 of 48,825 track
  rows were four months old, from song IDs Navidrome had since reassigned —
  verified server-side: of 9 old IDs on a sample album, **0 still existed**, while
  both current IDs did. Cause: the track pass iterated every album row in the DB
  instead of the albums Navidrome just returned, and nothing deleted rows that
  vanished. This poisoned the resolver: 17,955 duplicate `search_key` values
  (36.8% of the table) meant ambiguity was routine and dead IDs were in the mix.
  `full_sync` now prunes unseen tracks/albums/artists (batched, and refuses to
  run on an empty seen-set so a failed fetch cannot wipe the library). After the
  fix: 27,945 tracks, duplicates down to 994 (3.6%).

### Tests
93 → 195 passing (102 new), plus 22 pre-existing YouTube/Lidarr tests for 217
total. Coverage added for the 204 path, rate limiting and quota headers, cache
TTLs and empty-list caching, resolver ambiguity determinism, blend edge cases,
source-routing (Last.fm not contacted when ListenBrainz covers a run), and sync
pruning.

### Deployment & live verification (2026-11-19)
- Rebuilt and restarted the container; startup migration added both columns,
  backfilled `search_key` for **21,999/21,999** tracks, and created the cache
  tables and indexes
- Full resync populated `recording_mbid` (27,945 tracks over 2,934 albums) and
  pruned 20,880 stale tracks / 981 albums / 60 artists
- ListenBrainz live: token valid, **4 listens** recorded (scrobbling confirmed
  working), 0 CF recommendations (needs more history), bogus-MBID request
  correctly returns `[]`
- Recency trigger run end-to-end: reached ListenBrainz, logged the thin-history
  warning, fell through to Last.fm, and pushed a 50-track
  "Fresh Discoveries 2026-11-19" to Navidrome
- Expected steady state once history accrues: ~2 calls/run warm (listens +
  recommendations, both cached), ~22 on a cold run — within the 30/window budget

### Operational notes
- `tracks.recording_mbid` requires a **full library resync** after deploy; the
  startup migration only backfills `search_key`
- Config via Automation Hub → ListenBrainz (username + user token), plus a
  "Check" action that reports the account listen count and warns at 0

---

## 2026-11-19 — YouTube Music Playlist Import + Lidarr Queueing

### What it does
Paste a YouTube Music playlist URL and the app will:
1. Parse the playlist with `yt-dlp --flat-playlist` (metadata only, no download)
2. Fuzzy-match each track against the Navidrome library (three search
   strategies — title-only, artist+title, artist-only — scored with a blend of
   `SequenceMatcher` and stop-word-blind token overlap, threshold 0.75)
3. Queue artists with no local match to Lidarr via MusicBrainz lookup
4. Create the playlist in Navidrome, updating in place if the name already
   exists (delete-then-recreate, since `updatePlaylist.view` doesn't reliably
   replace tracks)

### Notes
- `Dockerfile` now installs Node.js — yt-dlp needs it to solve YouTube's JS
  challenges. Without it, playlist parsing fails on modern YouTube.
- Artist/title parsing strips the usual "(Official Video)", "(Lyric Video)",
  "(Remastered)" noise and falls back to the channel name (minus the "- Topic"
  suffix) when the title carries no artist.
- Album is blank from a flat playlist entry — it is only known after a Navidrome
  match, so Lidarr gets the artist, not a specific album.
- Cleanup during merge: a second `LidarrConfig` model had been declared on its
  own `declarative_base()` in `models/lidarr.py`. It mapped the same table but
  was absent from `Base.metadata`, so `init_db()` never created it — the table
  only existed because a script had made it by hand. Consolidated onto the main
  `Base` in `models/database.py`; `init_db()` is now the single source of truth.
- Scratch scripts (`fix_indent.py`, `kpop_test.py`, `similarity_test.py`,
  `fresh_discoveries_draft.py`, `update_newest.py`, `backup_scheduler.py`) and
  checked-in backups (`database.py.bak`, `lastfm.py.backup`) were left out of the
  merge — git history already covers those.

### Matching fixes found by testing a real playlist (2026-11-19)

Tested against a real 69-track playlist ("Break out the boom box"). The first run
matched 31 and looked like a 55% failure rate — but checking the library database
directly showed most "misses" were correct: the library genuinely lacks that
80s/90s rock (Soft Cell, Survivor, Queen's studio version, Def Leppard, ZZ Top,
Billy Idol, Cranberries and more are simply absent). The real defects were
narrower, and all four were fixed:

1. **Search queried the raw YouTube title.** Navidrome's index doesn't match
   "(2013 Remaster)" / "[2023 Remaster]" suffixes, so owned songs returned *zero*
   candidates. Titles are now cleaned before searching *and* on both sides when
   scoring, so a library copy carrying its own suffix
   ("Juke Box Hero (Foreigner 4 Stereo Remix)") still scores 1.0.
   Recovered: Separate Ways (Worlds Apart), Hungry Like the Wolf, Go Your Own Way,
   Juke Box Hero.
2. **Channel names were treated as artists.** "Queen Official", "Whitesnake TV",
   "HouseOfPainTV" and "SirMixAlotChannel" were compared against real artist names
   (destroying the 30% artist weight) and would have been sent to Lidarr as lookup
   terms. Now stripped — including glued forms — while protecting real names like
   "The Band".
3. **A right title with the wrong artist counted as a match.** "Celebration" by
   Kool & The Gang resolved to They Might Be Giants; "Jump" by Kris Kross to
   Rihanna; "Inside Out" by Eve 6 to Sara Bareilles. A title-heavy blend (0.7/0.3)
   lets an exact title alone clear 0.75. Added an artist gate at 0.45 similarity:
   no artist agreement, no match — the track is reported missing instead, which is
   honest, and Run-D.M.C.'s "Walk This Way" no longer silently becomes the
   Aerosmith original.
4. **Live takes won ties.** After cleaning, "Song", "Song (Live; 2010 Remaster)"
   and "Song (2001 Remaster)" all score 1.0 and the first from search won. Raw
   string similarity is the *wrong* tie-break — "2001 Remaster" resembles
   "Live; 2010 Remaster" more than it resembles the bare title, so it actively
   favoured the live copy. Now prefers the candidate needing no cleaning.

Result: 32 matched, **0 wrong-artist matches** (was 4), 4 false negatives recovered,
studio versions preferred. 37 tracks reported missing, of which ~35 are verified
absent from the library.

### Live run — "Break out the boom box" (2026-11-19)

Run against that real 69-track YouTube Music playlist through the deployed API:

- Parsed: 69 tracks, channel noise stripped ("Queen Official" becomes "Queen")
- Matched to the Navidrome library: **32**, correct artists, studio copies
  preferred over live/remaster variants
- Wrong songs: **0** — the artist gate rejected They Might Be Giants, Rihanna and
  Sara Bareilles, each of which had matched on title alone before the fix
- Missing from library: **37**
- Queued to Lidarr: **0 newly**. Every one of the 37 missing artists is already
  monitored ("Artist already in Lidarr" logged for each), and re-adding is
  skipped by design
- Navidrome playlist created: "Break out the boom box"
  (`id: 5EQ9NdhfPYC2RLMmdnsh9P`)

Absent artists stay flagged `missing` in the result so the UI can surface them,
while already-monitored artists are not re-added.

---

## 2026-11-19 — Playlist Retention (prune stale Fresh Discoveries)

### What it does
After the recency trigger publishes a dated playlist (`Fresh Discoveries
YYYY-MM-DD`), it now prunes playlists in that same series that are more than
**two weeks old** — deleted from Navidrome and from the `generated_playlists`
table. Before this, one playlist per day accumulated forever.

### Design
- Age comes from the **date in the playlist name**, not `updated_at`. The
  trigger rewrites today's playlist on every run, so `updated_at` says nothing
  about how old the *list* is.
- Only names sharing the reference name's prefix (date stripped) are
  eligible — `More Like This: …`, `Weekend Mix`, imported YouTube playlists and
  undated custom names are never touched. A trigger with a custom static name
  (no date suffix) has no series to age against and prunes nothing.
- `PLAYLIST_RETENTION_DAYS = 14` in `services/scheduler.py`. Cutoff is
  `reference_date - 14d`, exclusive: with today at 2026-11-19, 2026-11-05 is
  kept and 2026-11-04 is removed.
- Navidrome is deleted from **first**; the DB row is only dropped once that
  succeeded, so a failed API call leaves the row for the next run to retry
  rather than orphaning a playlist in Navidrome with no record of it.
- Pruning is wrapped so it can never fail a run that already produced a
  playlist: per-row errors are logged and skipped, and the whole step is in a
  try/except that logs a warning.

### Tests
241 passing (14 new in `test_triggers.py`): date parsing (including impossible
dates and nulls), window boundary at exactly 14 days, other series untouched,
undated rows inside the prefix untouched, Navidrome failure keeps the row,
rows without a Navidrome id dropped from the DB only, and a no-op run.
