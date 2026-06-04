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
- Phase 0: Project setup ✅ (in progress)
- Phase 1: Foundation (FastAPI, Navidrome client, SQLite, sync)
- Phase 2: Seed-based playlist generation
- Phase 3: Smart Triggers
- Phase 4: Frontend
- Phase 5: Polish & Deploy

---

## Phase 0: Project Setup

### What was done
- Created GitHub repo szehui/localsync-ai
- Scaffolded project structure (backend/, frontend/, docker-compose.yml, Dockerfile)
- Created this dev log
- Created wiki page for project context

### Issues encountered
_(none yet)_

---

## Phase 1: Foundation

### What was done
_(pending)_

### Issues encountered
_(pending)_
