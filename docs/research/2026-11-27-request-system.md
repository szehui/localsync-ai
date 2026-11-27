# "Seerr for music" — gap analysis and data-source evaluation

**Date:** 2026-11-27
**Status:** research complete, decisions pending, no code written
**Scope:** turn localsync-ai from a playlist generator into a request front-end for
Lidarr, with a browsable music catalog the way Seerr fronts Radarr/Sonarr.

Evidence for every API claim below is in `docs/research/catalog-probe.py` and the
Lidarr MBID probe run against the live instance on 2026-11-27 (Lidarr 3.1.6.5079).

---

## 1. What Seerr actually is (and which parts map to music)

Seerr is a *request broker* between humans and the \*arr stack. Its features, mapped
to a music equivalent:

| Seerr feature | Music equivalent | Notes |
| --- | --- | --- |
| TMDB-backed search + discover rails | MusicBrainz catalog + ListenBrainz rails | No free TMDB-equivalent; see §3 |
| Request movie / series / season | Request album / artist (discography) | Music's atomic unit is the **release group** |
| Request status: pending → approved → processing → available | Same, with `partially_available` for albums | Album = multi-track, so partial is a first-class state |
| Approval queue + granular permissions + quotas | Owner approves; per-user auto-approve flag | Household scale, not 500 users |
| Library scan for "already available" | Navidrome sync keyed on release-group MBID | Already 90% built — see §2.4 |
| Radarr/Sonarr status sync | Lidarr album stats + queue + history | Currently add-only, no read-back |
| Notifications (Discord/webhook/email) | One webhook (Discord) is enough | |
| Watchlist / blocklist | Per-user dismiss already exists; add a global blocklist | |
| Override rules | "Artist already in Lidarr → monitor the album, don't re-add artist" | Already implicit in `_add_album_core` |

The important asymmetry: **Seerr requests a title; we must request a release group.**
"Random Access Memories", the 2023 Drumless Edition, and the Vanderway Edit single are
three distinct MusicBrainz release groups with three different MBIDs. The UI has to
show year + type + track count so the user picks the right one, and the request has to
carry the MBID — not a title to be fuzzy-matched later.

---

## 2. Gaps in localsync-ai today

Ordered by how much they block a Seerr-like product. Code pointers are into
`backend/app/`.

### 2.1 No catalog layer at all

There is no external music catalog, no search endpoint, no browse rails. The only
"discovery" is `services/discovery.py`, which inverts the *playlist* resolver: it takes
ListenBrainz similarity candidates that failed to resolve locally and offers them. That
is a recommendation feed derived from one user's listening — not a searchable catalog,
and it cannot answer "find me the album my friend mentioned."

**Gap:** a catalog client + `GET /api/catalog/search`, `/album/{mbid}`, `/artist/{mbid}`,
`/rails`.

### 2.2 No request entity or state machine

`DiscoveryOffer` is the closest thing, and it is the wrong shape:

- keyed on `recording_mbid` (a track), not a release group;
- statuses are `offered | saved | dismissed` — "saved" means "we POSTed to Lidarr",
  nothing after that;
- `lidarr_album_id` is written once and never read again;
- it is per-user opinion state, not a household request queue. Two people wanting the
  same album produce two independent rows, and one person's dismissal is not visible
  to the other.

**Gap:** `requests` + `request_votes` (second requester = vote, not a duplicate add),
plus a status lifecycle and an approval queue.

### 2.3 Lidarr integration is write-only and title-based

`services/lidarr.py` has a genuinely good add path (artist-aware lookup, single-album
monitoring collapse that converges, transient-503 retries) — but:

- **It resolves by title.** `_add_album_core(artist_name, release_name)` →
  `lookup_album_for()` → `_pick_album()` with fold/substring/difflib heuristics. Every
  one of the documented incidents (Shang-Chi → *Savage Mode*, "Blur" → "Essential Blur",
  ENHYPEN → SEVENTEEN) lives in that code path. The heuristics are correct as a
  *fallback*, but they should not be the primary route.
- **Nothing reads Lidarr back.** No `statistics.percentOfTracks`, no queue, no history.
  A request can silently fail (no indexer match, import rejected) and nobody knows.
- **Album ids are unstable** and the code works around that by re-resolving in the same
  breath. Requests must store the *release-group MBID*, never the Lidarr album id, as
  their durable key.

**Gap:** MBID-first add + a reconciler that reads Lidarr state into request statuses.

### 2.4 Availability is track-level, and albums carry no MBID

`albums` has `id/name/artist_name/year/...` and **no MusicBrainz id at all**. Only
`tracks.recording_mbid` exists (~55% coverage, clustered per-album per the earlier
migration work). So "do we already own this album?" can only be answered by fuzzy name
matching today — the same class of bug as 2.3.

Verified fix, and it is cheap: **Navidrome's native REST album list already includes
`mbzReleaseGroupId` and `mbzAlbumId` on every row** (probed 2026-11-27). One paged
`/api/album?_start&_end` walk over ~3.1k albums populates a new
`albums.release_group_mbid` column with no per-album calls. The Subsonic API — which is
what `services/navidrome.py` uses — exposes only `musicBrainzId` = the **release** MBID
(`MbzAlbumID`), which does *not* match Lidarr's release-group key. That mismatch is
exactly the kind of thing that produces a system that is silently always-wrong.

Measured coverage in a random sample of 12 library albums: **5/12 (≈42%)** carried a
release-group MBID (tagging is per-album, all-or-nothing — same shape as the recording
MBID work). So MBID availability is the *primary* signal with a name-keyed "probably
owned" fallback, never an auto-deny.

### 2.5 Permissions are `is_owner` and nothing else

`users.is_owner` gates user management. There is no "may request", "auto-approve",
or quota concept. Delegated Navidrome auth means the user set is already correct; only
the request permissions are missing.

### 2.6 No notifications, no blocklist, no quotas

Nothing to reuse. All are small additions once the request entity exists. Per-user
dismiss (`discovery_offers.status`) is a good basis for a household-level blocklist.

---

## 3. Data source evaluation (probed live, 2026-11-27)

### Requirements

1. Text search with tolerable typeahead latency
2. Browse rails (trending / new releases / by artist)
3. **A stable id that Lidarr understands natively**
4. Artwork
5. Popularity signal for ranking
6. No API key, no ToS landmine, self-hostable, rate budget a household can live with

### Candidates

| Source | Search | Rails | Lidarr-native id | Art | Popularity | Key | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **MusicBrainz WS/2** | yes (Lucene, exact-ish; typo query still ranked RAM #1) | browse by artist; no global trending | **yes — release-group MBID is Lidarr's `foreignAlbumId`** | via Cover Art Archive | no | none, UA required | **primary catalog** |
| **Cover Art Archive** | — | — | keyed by RG MBID | yes (1200/500/250 thumbs) | — | none | **artwork** |
| **ListenBrainz** | weak | **yes** — `explore/fresh-releases` (2 MB/14 d), `popularity` (POST, MBID lists) | yes (MBIDs throughout) | partial (`caa_id`) | **yes** — `total_listen_count`, `total_user_count` | token optional | **rails + ranking** |
| **Deezer** | **strong, fuzzy, fast** | charts | **no MBID anywhere** (checked album + artist objects) | yes | fans count | none | optional typeahead/preview only |
| Last.fm | yes | charts | occasionally, unreliable | yes | yes | key (already have) | keep for existing similarity, not catalog |
| Spotify | best | best | no MBID in API | yes | yes | client credentials + ToS | reject |
| Discogs | yes | no | no MBID | yes | marketplace | token | reject |
| iTunes | yes | no | no MBID | yes | no | none | reject |

### Verified endpoint behaviour

- `GET /ws/2/release-group/{mbid}?inc=artist-credits+releases` → title, primary/secondary
  type, first-release-date, artist credit **with artist MBID**, and all 23 editions.
- `GET /ws/2/release-group/?query=releasegroup:"nevermind" AND artist:"nirvana"` → clean
  results with `score` 100. Unquoted/loose queries return six-figure `count` values and
  rank by score — usable, but the UI must show enough metadata to disambiguate.
- Rate limit: `X-RateLimit-Limit: 400`, resets per window, **1 req/s sustained**; one 503
  observed under burst. Client must serialise + retry (same pattern as
  `services/lidarr.py`'s backoff and the ListenBrainz 1 req/s throttle).
- Cover Art Archive: 200 + image list for a well-tagged RG; 404 for untagged ones.
- ListenBrainz `popularity/release-group` and `popularity/artist` are **POST-only** and
  take `release_group_mbids` / `artist_mbids` lists (GET returns 405; wrong field name
  returns 400). Returns `total_listen_count` + `total_user_count`.
- `explore/fresh-releases?days=N` returns `release_group_mbid`, `artist_mbids`,
  `release_date`, `caa_id`, and `listen_count` — a ready-made "new this week" rail.

### Decision

**MusicBrainz is the catalog and the request key. ListenBrainz supplies rails and
popularity. Cover Art Archive supplies artwork. Deezer is optional enrichment, never a
request key.** Reasons: MBID identity is the whole game (it removes the fuzzy-matching
failure class and gives exact "already owned" checks), and the household already runs
ListenBrainz for every user, so the rails cost no new integration.

---

## 4. The finding that shapes the architecture

**Lidarr resolves a release-group MBID directly.** `album/lookup` passes the term to
`SkyHookProxy.SearchForNewAlbum`, which has an explicit MBID branch
(`IsMbidQuery` → `lidarr:` | `lidarrid:` | `mbid:`). Confirmed present in our installed
version (v3.1.6.5079 source) and **verified live**:

```
GET /api/v1/album/lookup?term=mbid:aa997ea0-2936-40bd-884d-3af8a0e064dc
→ HTTP 200, exactly 1 row
   title: Random Access Memories
   foreignAlbumId: aa997ea0-…  (matches request)
   artist: Daft Punk | foreignArtistId: 056e4f3e-…
   images: 2 | releases: 23 | albumType: Album
```

The returned object is the same shape the existing `POST /api/v1/album` add path already
consumes (it carries the artist resource, images, releases, profiles get injected by
`_add_album_core`). So a request can go:

```
release_group_mbid → lookup?term=mbid:<uuid> → POST /album → monitor-only collapse
```

with **zero title matching**. `_pick_album()` stays as the fallback for the YouTube
import, not as the request path.

Consequence for the data model: the durable key of a request is the release-group MBID.
`lidarr_album_id` is a cache that can vanish at any metadata refresh and must never be
the lookup key for anything.

---

## 5. Proposed shape

### 5.1 Request lifecycle

```
pending ──approve──▶ approved ──▶ downloading ──▶ available
   │                    │              │
   └──deny──▶ denied    │              └──▶ partially_available
                        └──▶ failed  (no release found / import failed)
```

- `approved` — in Lidarr, monitored, 0 tracks on disk, no queue entry.
- `downloading` — queue entry exists, or history shows `grabbed` without import.
- `available` — Lidarr `statistics.percentOfTracks == 100`, **or** Navidrome has the
  release-group MBID (media-server truth wins; it means you can actually play it).
- `partially_available` — `0 < percentOfTracks < 100` (common for deluxe editions).
- `failed` — history shows `downloadFailed`/`importFailed` since the request, or no
  release was found after 48 h. Surfaced with the reason, retryable by the owner.

### 5.2 Data model (SQLite, inline migrations in `init_db()`)

```
requests
  id, user_id, release_group_mbid, artist_mbid, artist_name, album_title, year,
  primary_type, cover_url, status, note,
  requested_at, decided_at, decided_by, lidarr_album_id, lidarr_artist_id,
  last_synced_at, last_error
  UNIQUE(release_group_mbid) WHERE status NOT IN ('denied','cancelled')

request_votes(id, request_id, user_id)          -- 2nd requester = vote, not a 2nd add
blocklist(mbid, artist_mbid, scope, reason, created_by, created_at)
catalog_cache(key, kind, payload_json, fetched_at)   -- search/album/artist, TTL by kind
albums.release_group_mbid, albums.mbz_album_id       -- new columns + index
users.can_request, users.auto_approve                -- new columns
```

### 5.3 Backend pieces

- `services/musicbrainz.py` — serialised client (1 req/s, UA, 503 backoff, ETag-less
  cache), `search_release_groups`, `get_release_group`, `get_artist`, `browse_artist`.
- `services/coverart.py` — RG MBID → front image URL, 404-tolerant, cached.
- `services/lb_rails.py` — fresh releases + popularity lookups (reuses the existing
  ListenBrainz client and its rate limiter).
- `services/catalog.py` — merge MB + CAA + LB popularity into one result shape; mark
  owned/requested per caller; filter bootlegs/compilations by default.
- `routers/catalog.py` — search / album / artist / rails.
- `routers/requests.py` — create, list (mine/all), approve, deny, cancel, retry.
- `services/requests.py` — create/dedupe/vote, MBID→Lidarr push (reuses
  `LidarrService._add_album_core` via a new `lookup_album_by_mbid`),
  status reconciler.
- `services/sync.py` — native-API MBID pass filling `albums.release_group_mbid`.
- reconciler job on APScheduler (5–15 min, open requests only).

### 5.4 Frontend

New tabs alongside the existing six in `App.tsx`:

- **Search** — typeahead over MB (debounced 400 ms), album detail sheet with
  year/type/track count/edition count, "Request" or "In library" / "Requested by X".
- **Requests** — my requests with live status; owner sees all + approve/deny bulk.
- **Discover** — reuse the existing feed, but "Add" becomes "Request" (goes through the
  same request entity instead of straight to Lidarr).

---

## 6. Open decisions

1. **Approval model** — auto-approve every household member (Seerr's default posture) vs
   queue everything for the owner vs per-user flag (default off for new users).
2. **Granularity** — album-only for v1, or also "whole discography" artist requests
   (Lidarr `artist/lookup?term=mbid:<artist>` + add with monitor policy)? Artist requests
   are the single easiest way to accidentally queue 30 albums.
3. **Scope of v1 rails** — just search + requests, or also new-releases/trending on the
   landing page (adds the LB rails work, ~1 day).

---

## 7. Phased outline (after decisions)

- **Phase 0 — spike (½ day).** Script the MBID add path against live Lidarr, including
  the two known-unreachable cases (Various Artists soundtracks, Korean-titled K-pop) to
  see whether the MBID route fixes them. Populate `albums.release_group_mbid` from the
  native Navidrome API and measure real coverage on the 3.1k-album library.
- **Phase 1 — catalog (2–3 days).** MB/CAA/LB clients + cache + filters, catalog router,
  tests for rate limiting/backoff/degraded-upstream.
- **Phase 2 — requests (2–3 days).** Schema + migration, create/approve/deny, MBID→Lidarr
  push, dedupe/votes, permissions.
- **Phase 3 — truth (1–2 days).** Reconciler pulling Lidarr album stats/queue/history and
  Navidrome availability; status transitions tested against recorded fixtures.
- **Phase 4 — UI (2–3 days).** Search, album detail, requests, owner queue.
- **Phase 5 — polish.** Discord webhook, blocklist, quotas, discovery→request wiring.

Total: roughly 8–11 working days, of which Phase 0 is the only part that can invalidate
the design — do it first.
