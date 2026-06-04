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
- Phase 4: Frontend (pending)
- Phase 5: Polish & Deploy (pending)

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
