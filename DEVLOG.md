# LocalSync AI — Development Log

## 2026-11-28 — Phase 5: notifications and the blocklist

Two gaps closed, and a bug that had been breaking discography requests since
Phase 3.

**Request parents were failing every five minutes.** An artist request carries no
release-group MBID — its children do. The reconciler treats "the album is gone
from Lidarr" as `failed`, so every cycle it looked up an album that could never
exist and marked the parent failed while its children downloaded happily. Parents
belong to `_roll_up_parents`, which derives their status from the children; the
per-row pass now skips them. Nothing caught this because no discography request
had been reconciled in production yet, and the rollup test never asserted that
the row pass left parents alone.

### Notifications

A Discord webhook, owner-configured, three events: `request.created`,
`request.available`, `request.failed`. Fired from the router on creation and from
the reconciler on transition, collected during the cycle and sent after the
commit — an HTTP call inside the per-row savepoint would hold a write lock for
the length of a network round trip.

**Once per status, not once per cycle.** The reconciler runs every five minutes
over every open request, so a naive implementation announces a failure forever.
`requests.notified_status` records what was announced; `retry` clears it, because
a second attempt deserves its own notification.

**Discographies announce once.** Children of an artist request stay quiet; the
parent fires when the last album lands. Thirty individual "album available"
messages for one ask is spam, and the parent is the thing the user asked for.

**The webhook URL is a credential**, so it is write-only over the API: `GET`
returns `http://172.22.0.1:8899/…` and never the token, the log line carries the
same mask, and a `PUT` without a URL keeps the stored one so the form can be
saved without re-pasting a secret.

**Two UI bugs found by watching it fail.** The `Enabled` checkbox defaulted
unchecked for an unconfigured webhook, so "Save" stored `enabled=false` and only
the test button worked — the test endpoint ignores the flag by design, which is
exactly what made the bug invisible. And the API reported *subscribed* events
rather than stored ones, so pausing blanked the checkbox list and resuming would
have come back with nothing ticked.

### Blocklist

Owner-only, two scopes: one release group or a whole artist. Enforced in four
places, because hiding is not refusing: creation is refused server-side (a client
that never saw a filtered search must still be told no), search drops blocked
cards, rails drop them, and artist pages keep them — greyed, labelled, not
requestable — since an album silently missing from a discography reads as a bug.
The artist plan skips blocked albums and reports how many it skipped.

### Verified

Against a real HTTP sink and the live stack: test message delivered as a
well-formed embed; `New request: Portishead — Dummy` arrived with artist, year,
requester and cover art; blocking *Third* removed it from search results while
the artist page kept it greyed; unblocking restored it; a member account got 403
from both new APIs and does not see the Manage tab. `1,003` backend tests pass.

Discography completion was verified with a fake Lidarr in tests, not live — no
download finished during the session, so `request.available` has never been seen
end to end. Worth knowing.

## 2026-11-28 — Phase 3: status truth

Requests now report what is *true* instead of what was asked for. A row moves
`approved → downloading → imported → available`, and every step is evidence from
Lidarr or the library, never from the request itself.

**The failure mode this exists to prevent.** Phase 2 could say "approved" and
mean nothing: Lidarr accepted the album, then found no release, and the row sat
there forever. Worse, `POST /album` makes Lidarr create the artist *and* its
whole discography, so a matching album row can exist having never been asked for.
An album row is not evidence. Album **statistics** are.

**One fetch each, not one per request.** The queue (42 records live) and the
history (43,749 records, top 100 read) are pulled once per cycle and indexed by
album id. Ten open requests used to mean ten identical queue pulls.

**The ladder only goes up.** Lidarr removes a record from the queue *before* the
importer writes the files, so a cycle that sees an empty queue and no files must
not demote `downloading` to `approved`. `failed` is the one state that can be
left, and only on positive evidence (files appeared) — silence never undoes a
failure, because that is what the retry action is for.

**Honest about the last mile.** Files in Lidarr but not yet scanned by Navidrome
is `imported`, not `available`: the library cannot play it. Verified live on
*Coldplay — A Rush of Blood to the Head*, which Lidarr holds completely and
Navidrome does not have — one instance of the pre-existing 499-album gap, now
visible instead of invisible.

**Two real bugs found on the way.** `catalog.OPEN_STATUSES` read
`("pending", "approved", "processing")` — `processing` is not a status in this
state machine, and `downloading`/`imported` were missing, so a card for an album
already being downloaded still offered "Request". It is now derived from the one
status machine in `services/requests.py`. And `albumImportIncomplete` fires on
every partial import (3 of the 5 most recent live history events) — treating it
as a failure would have marked healthy downloads dead.

**Deliberate deviation:** a vanished album is reported `failed` ("no longer in
Lidarr — retry to add it again") rather than silently re-added. Re-adding an
album someone deleted in Lidarr is resurrecting content they removed on purpose.

Deployed and verified live: `available` via MBID evidence (TWICE — *&TWICE*),
`downloading` from the real queue (*2Pac — Better Dayz*), `imported`
(*A Rush of Blood to the Head*), second cycle changed 0, idle cycle returns zeros
in 33 ms with no Lidarr calls, `POST /api/requests/sync` owner-only (401 anon).

909 tests (70 new). Phase 4 (the UI) is next; nothing in the browser exposes
any of this yet.

## 2026-11-28 — Phase 4: the browser UI, and three bugs it exposed

Search, album detail, artist discography, and the requests queue are usable by
someone who is not holding curl. Nothing here is new *logic* — the point is that
the phase-1-through-3 machinery had never been driven by the frontend, and
driving it surfaced three production bugs that 909 green tests did not.

### 1. `/catalog/rails` returned 500

`lb_rails` parsed ListenBrainz popularity as `{mbid: count}`. The live API
returns a **list of objects**:

```
[{"artist_mbid": "...", "total_listen_count": 77081947, "total_user_count": 366682}]
```

The unit tests encoded the same wrong assumption as the code, with fixtures
written from it, so they passed while the endpoint 500ed on first live call.
This is the failure mode a fake client invites: it asserts the shape you
imagined. Both shapes parse now, and the fixtures carry the live payload
verbatim (with a comment saying so, and where it came from).

### 2. Every rail card rendered a blank title

The fresh-releases feed has **no `release_group_name` field, and no `title`
either** — zero of 3,597 live rows. Albums are named in `release_name`; the code
read the two keys that do not exist and fell back to `""`. Same root cause as
above: the test fixture invented the keys.

Also fixed: 72% of rows carry `caa_release_mbid`, so artwork is now built from
it directly (CAA redirects to the image, so it is a usable `<img>` src with no
API call) instead of a per-album Cover Art Archive lookup.

### 3. MusicBrainz was unreachable — a network bug, not a code bug

Every search returned `503 MusicBrainz is busy`. The cause: MusicBrainz resolves
to a Hetzner IPv6 address first; **Docker's default bridge is IPv4-only**; and
on this network the IPv4 path to `142.132.241.153` stalls mid-TLS-handshake
(`unexpected eof while reading`) from the host *and* from containers, while the
IPv6 path is healthy. PMTU was ruled out (full-size ICMP passes), SNI filtering
was ruled out (no-SNI handshake to the bare IP fails identically), and no tunnel
is involved (`ip route` is plain wifi via the router).

Fix: the compose network is dual-stack (`fd00:22:1::/64`). Docker sets up NAT66
itself — no host firewall or sysctl changes. Both upstreams return 200 from
inside the container now. Worth remembering: this class of failure looks exactly
like an application error from the logs, and the app's own 503 mapping is what
made it look deliberate.

### Fixed while wiring

- **Rails returned 3,597 cards in 37 seconds.** A rail is browsed, not
  paged. Now sliced to 24 after sorting newest-first, with a 200-row pool for
  ranking. ~3s.
- **"Trending" ranked by artist popularity**, so the rail for *new releases*
  surfaced Queen and Depeche Mode — artists with large back-catalogue followings
  who happen to have a release this fortnight. It ranks by release-group listens
  now, which is what the label claims.
- **Search never used the cache module it imported.** Every debounced query cost
  a MusicBrainz request. Upstream payloads are cached for 10 minutes; ownership
  and request badges are deliberately *not* cached, so an album requested a
  minute ago does not reappear as requestable. The cache key is versioned
  (`v2`) because the first version of this change was invisible for ten minutes
  — every query cached under the old behaviour served an empty artist list.
- **Artist suggestions were only offered when nothing matched.** "twice"
  returns nine albums titled *Twice* by unrelated artists and buries TWICE;
  there was no route from that page to the band. The strip is unconditional.
- **`/auth/me` had no permission fields.** The UI needs `is_owner` to decide
  whether to show Approve; discovering it from a 403 is a worse answer than not
  showing the button. Read from the database on every call, never from the JWT.
- **Retry appeared on approved requests.** Approved requests are already moving
  — the reconciler owns that, and "Check now" is how you force it. Retry is for
  `failed`.

### Verified in the browser, end to end

Signed in (token injected into localStorage — no password handled by the
browser), home rails render with titles and covers; *Season of Bloodshed* opened
in the modal; **Request album** flipped the badge to **Approved** with "Sent to
Lidarr, waiting for a download"; the database agreed (`request 1,
lidarr_album_id 4894`). Requests tab showed the row with owner and relative
time. Searching "twice" showed the artist strip above the results; clicking
TWICE opened 39 albums with "9 in your library" and a scope selector that
re-planned from **2 to request** to **30** on switching to albums+EPs.

Cleanup: the test created artist 506 *and* 5 albums (`POST /album` creates the
whole discography), all removed; the request row deleted; the queue is empty
again. The cleanup script guards artist deletion on the artist having been
created in the last two hours — a sibling-count check would have been wrong,
because the request produces five siblings, not one.

922 tests (13 new), including `test_catalog_rails.py`, which exercises the rails
*composition* — the old rails tests mocked the service at the router boundary,
which is how a 500 hid behind a green suite.

## 2026-11-28 — Phase 2: the request write-path (music Seerr)

Requests, approval, and the Lidarr push. Everything is keyed by **release-group
MBID**; the title-based `album/lookup` path is never used for a request.

### Why MBID, concretely
Phase 1's discovery "Add" called `add_single_album(artist_name, release_name)`.
Live evidence of the failure: the *Shang-Chi* soundtrack resolved to **Savage
Mode** — right artist, wrong album, queued for download while the UI said
"Added to Lidarr". `album/lookup?term=<names>` ranks by relevance and the top hit
is whatever MusicBrainz thinks is closest. A request therefore resolves its
release group first (`album/lookup?term=mbid:<uuid>`) and refuses when Lidarr has
no row for *that* id (`unresolved`), rather than substituting a neighbour.

### Data model
- `requests` (+ `request_votes`, `blocklist`): one row per ask, with the display
  fields cached so the list and approval need no upstream calls.
- **Partial unique index** `ix_requests_open_rg` on `(user_id, release_group_mbid)
  WHERE kind='album' AND status NOT IN ('denied','cancelled')`. A second ask for
  the same album becomes a **vote** (201, same id) instead of a duplicate Lidarr
  add; the index is what makes that race-safe, so the insert path catches
  `IntegrityError` and converts it to a vote rather than pre-checking and hoping.
- `users.auto_approve` / `users.can_request`. Owner defaults to auto-approve,
  members do not — matching Seerr: members ask, the owner approves, and the owner
  does not queue requests for their own permission. The owner UPDATE runs only in
  the one-time "column is new" window so a deliberate opt-out survives restarts.

### Lifecycle
`pending → approved → downloading → imported / partially_available → available`,
with `denied` (owner) and `cancelled` (requester or owner) as the only states that
free the release group for a new request. A transient Lidarr failure stays
`approved` with `last_error` set rather than becoming `failed`, because 503 from
Lidarr's MusicBrainz proxy is throttling, not a verdict.

### Found live, fixed
MusicBrainz answers **400**, not 404, for a well-formed UUID it has never seen, so
the client's `allow_404` flag did not cover it and `POST /api/requests` returned an
HTML 500 page on a bogus MBID. Now translated: 4xx → `404 not_found`, 5xx/transport
→ `503 upstream`. Both pinned by tests.

Also folded in here (deferred from the previous entry): **`discovery_state` is now
per-user**, `user_id` primary key, legacy singleton row attributed to the owner.
Under multi-user the singleton showed whichever user refreshed last, so one
person's error could appear on another's Discover page.

### Verified live, against the real stack
Minted a session token and drove the running app:
- `POST /api/requests` → 201 pending with real MusicBrainz metadata (Boards of
  Canada — *Hooper Bay*); Lidarr album count unchanged while pending (4753 → 4753)
- duplicate ask → 201, **same request id** (vote, not a second row)
- `approve` → 200 approved, `add_albums_by_mbid` resolved the MBID and monitored
  the album; `monitoring_ok` verified against `GET /album`
- bogus MBID → 404 `not_found`, nothing created
- `lookup_album_by_mbid` on an owned MBID → found; on a bogus one → `None`

Test album removed from Lidarr afterwards (`DELETE /album/4893?deleteFiles=false`,
4754 → 4753, confirmed gone); test request rows removed.

One thing worth knowing: Lidarr's `RefreshArtist` runs asynchronously after an add
and can re-create album rows unmonitored moments after a collapse pass. The
monitor pass therefore *verifies* against `GET /album` and reports
`monitoring_ok=False` instead of assuming success; a retry converges.

**839 tests passing** (769 at Phase 1 close; 70 new).

## 2026-11-27 — Duplicate tenant from a second Navidrome URL

Discovered page showed "no seeds available: this user has no played tracks with a
recording MBID". The message was accurate but the cause was not the library: there
were **two tenants named `sze`**. The owner row (`id=1`,
`https://navidrome.hui-family.org`) held all 618 annotations and 255 discovery
offers; a second row (`id=2`, `http://192.168.4.205:4533`, created minutes earlier
by a login) held nothing but an empty ListenBrainz config. The session was scoped
to the empty one, so discovery correctly found no seeds.

`get_or_create_user` keys on `(url, username)`, and that is deliberate — the same
username on two real Navidrome servers must be two tenants. But one server is
routinely reachable at two addresses (public hostname *and* LAN IP), and the key
cannot tell that apart from two servers. Logging in at the second address quietly
minted a second account with none of the user's history.

### Decision (reverses the earlier contract, on evidence)
A login for a username that already exists at a **different URL** is now refused
with `409` and a message naming the URL that owns it. Genuinely separate servers
still work as long as the usernames differ. The tradeoff is explicit: a real
second server that reuses a username now needs the row handled by an owner, which
is a rare, visible, recoverable case — whereas the silent duplicate was invisible
and cost a user their whole history. The tests that asserted the old
"two tenants" behaviour were updated rather than deleted, so the reversal is
recorded where the rule lives.

### Data repair
Deleted the empty duplicate (`id=2`) and its orphaned `listenbrainz_user_config`
row after asserting it owned zero annotations and zero offers. The owner tenant
kept `https://navidrome.hui-family.org`. Stale `discovery_state` error cleared.
Verified afterwards: `select_seeds(user 1)` returns 20 seeds (one per artist,
capped) and `select_seeds(<unknown user>)` still returns 0 — the guard against
empty history is intact, it just has history to work with now.

### Notes
- `discovery_state` is a **singleton** row, not per-user. With multi-user auth the
  UI therefore shows whichever user refreshed last, and one user's failure can
  appear on another's page. Pre-existing; worth making per-user (or keyed by
  `user_id`) before more than one person uses Discover.
- 757 tests passing (10 new for the login guard).

## 2026-11-27 — Music requests, Phase 1: the catalog

The read-only half of "Seerr for music": a user can now search MusicBrainz, browse
an artist's discography, and see — per card — whether the album is already in the
library and whether someone has already requested it. Nothing writes to Lidarr
yet (that is Phase 2).

### The decision that made this viable
Availability is keyed on the **release-group MBID**, never on a title. Lidarr's
title lookup returns the wrong album for compilations (a "Greatest Hits" request
added a different artist's) and fails outright for non-Latin titles. But this
library only carried a release-group MBID for 36.8% of albums — the rest were
invisible to an MBID-keyed catalog.

The fix costs **zero MusicBrainz calls**: Lidarr already resolved a release group
for every album it tracks and stores it as `foreignAlbumId`. Folding `artist|title`
on both sides and copying ids where the key maps to exactly one release group
resolved **1,210 of 1,524 matchable untagged albums (79.4%)**, lifting coverage
36.8% → 70.2%. Twenty sampled copies were validated against MusicBrainz: 18 exact,
1 edition variant, 1 artist-credit variant (`Pharrell` vs `Pharrell Williams`),
**zero wrong release groups**.

### Decisions
- **Backfill is a separate job** (`scripts/backfill_mbids.py`), not part of the
  library sync. It is a bulk copy plus a ~400-call MusicBrainz tail; a sync should
  not block on 15 minutes of upstream calls.
- **`mbid_checked_at` distinguishes "never looked" from "looked, found nothing."**
  Without it every re-run re-queries the ~800 albums that genuinely have no
  release group. It is also how the job resumes and how the tail tier records the
  structural floor (716 single-track rips, 135 `[Unknown Album]` tags).
- **Name matching is a second ownership tier, not a fallback to be avoided.** It
  needs no MBID, so an untagged rip still answers "probably in your library"
  (`owned_via: "name"`) instead of offering a duplicate request.
- **Ambiguity is skipped, never guessed.** A folded key mapping to two release
  groups is the Greatest-Hits collision; those rows go to the tail instead.

### Traps encoded in the code (each a measured failure, not a hypothetical)
- `albums.year` was **NULL for all 3,626 albums** — the Subsonic album object has
  no `year`. `maxYear` from Navidrome's native API is the only usable date, and it
  is now captured. A rule reading `year` would have silently disabled its own
  date check.
- **Year and track count never veto a match.** Run DMC's *Raising Hell* has
  `maxYear` 1999 against a 1986 release group (a reissue); Notorious B.I.G.'s
  *Doing It Real Big* has 4 local tracks against 14 (a partial rip). Both are
  correct matches that strict gating rejected.
- **`싸다9` → `9`** if you de-accent with NFKD and forget to recompose: NFKD splits
  Hangul syllables into jamo, which fall outside every CJK range. Recomposing with
  NFC keeps the glyphs.
- **CJK queries need a different shape:** `releasegroup:"화양연화 pt.1"` returns
  503; `releasegroup:(화양연화 pt 1)` works. `normalize_query` rewrites it.
- **`release-group` browse takes one `type` per call** — repeating the parameter
  returns 503.
- A release group's localised titles are *release* titles (`싸다9` has one titled
  `PSY 9th`). Harvested into `album_aliases` on first detail view, so the name a
  user remembers resolves locally without an upstream call.

### Verification (real data, not mocks)
- **747 tests passing** (196 new).
- Live MusicBrainz: search (`Random Access Memories`, 7,506 count), release-group
  detail, artist search and album browse all returned real payloads through the
  new client, with the 1 req/s limiter.
- Oracle tier end-to-end on a scratch DB with real Lidarr-shaped rows: exact match
  → `high`, `Hysteria (Deluxe Edition)` → `high`, two *Greatest Hits* → skipped as
  ambiguous, `八度空间` → left for the tail.

### Known limitation
Search results are presented in MusicBrainz relevance order. Ties at score 100 are
not re-ranked by popularity, so a user searching "Random Access Memories" sees two
cover versions before Daft Punk's. Worth a ListenBrainz-popularity re-rank once the
rails have cache warmed.

### Deployed and run against production (same day)
Rebuilt the container from `77441dd` and ran the backfill in place. The migration
added the five `albums` columns and both new tables on the live DB without incident
(backup at `~/localsync-db-backup-20261127-201148.db`).

| tier | albums resolved | MusicBrainz calls |
|---|---|---|
| Navidrome tags | 1,322 | 0 |
| Lidarr oracle | 1,030 (17 ambiguous skipped, 758 unmatched) | 0 |
| MusicBrainz tail | 42 (39 high, 3 medium) | ~600, 178 artists |
| **total** | **2,394 of 3,127 = 76.6 %** | |

The tail's 471 `skipped_singles` and 168 `unmatched` are the structural floor
predicted in Phase 0. `never_checked` is now 0, so re-runs are no-ops unless
`--retry-failed` is passed.

**Live ownership verified** against the real library: `Hot Space` (Queen),
`(What's the Story) Morning Glory?` (Oasis), `Good Girl Gone Bad` (Rihanna) and
`Hysteria` (Def Leppard) all report `owned: true, owned_via: "mbid"`, while
`Good Girl Gone Bad Live` and `Good Girl Gone Bad: The Remixes` correctly report
not-owned. App health held at 2–3 ms throughout the backfill's writes, and
`/api/catalog/*` answers 401 unauthenticated while unknown paths answer 404.

### Gap found in production (pre-existing, not caused by this work)
The `albums` table holds 3,127 rows but Navidrome reports 3,626 albums: Subsonic's
`getAlbumList2` omits ~499 albums that the native API knows about. Those albums are
invisible to the whole app (playlists too), and since `full_sync` prunes anything
Subsonic did not return, inserting them from the native pass would be undone on the
next sync. Ownership therefore cannot report 14 % of the library — worth
root-causing separately (likely albums whose files are missing, or a music-folder
visibility difference) rather than papered over here.


## 2026-11-27 — Multi-user support

Household members now get their own playlists, from their own listening history.
Before this, "logging in as someone else" silently *replaced* the only tenant.

### What was actually broken
The auth layer looked multi-user (a JWT per login) but the tenancy was a lie:
`ConnectionConfig` was a singleton row (`id=1`), login overwrote it, and
`get_current_user`'s identity was discarded at every call site. Consequences:

- A second person logging in and syncing **overwrote everyone's play counts**,
  because `Track.play_count` was a single column while Navidrome's `playCount`
  is per Subsonic account.
- `POST /logout` **deleted the shared credentials row**, so one person logging
  out stopped everyone's scheduled playlists.

### Decisions
- **Annotations split out** into `track_annotations(user_id, track_id)` with a
  composite PK, mirroring Navidrome's own `annotation(user_id, item_id)`. Library
  metadata (title/artist/album/genre/MBID) stays global — it is user-independent.
- **Legacy data adopted, not discarded.** Whoever was in `ConnectionConfig`
  becomes `users.id=1` with `is_owner=True` and inherits the existing play
  counts. All-zero tracks are skipped (indistinguishable from "no row", and one
  insert per track otherwise).
- **`lb_similar_cache` stays global; only CF recommendations go per-user.** Labs
  similar-recordings describe a *recording's* neighbours — a property of the
  recording, not the listener. CF recommendations are computed from one
  listener's history, so sharing those would recommend one person's taste to
  another. This corrects the original plan, which said to key both.
- **Logout no longer deletes credentials.** It drops only that user's live
  client; an owner-only `DELETE /api/auth/users/{id}` is how an account is
  actually removed. Scheduled generation survives a logout.
- **Playlist names are not prefixed.** Each user's client creates the playlist in
  their own Navidrome account, so two users can both own "Fresh Discoveries
  2026-11-27". Local rows are disambiguated by `owner_id`.
- **Triggers return 404 (not 403) for another user's row**, so the API does not
  reveal that someone else's trigger exists.

### Upgrade-path bugs found and fixed during the work
1. `Base.metadata.create_all()` never alters existing tables. On a deployed
   database `owner_id` was missing from `smart_triggers` / `generated_playlists`,
   so `init_db()` died with `no such column: owner_id` and the container
   crash-looped before serving a request. Added the `ALTER TABLE` step *and* the
   indexes (which `ALTER TABLE` cannot carry). Pinned by
   `test_migration_upgrade_path.py`, which builds the pre-multi-user schema by
   hand — a fresh-schema test can never see this class of bug.
2. `library.py` still returned and sorted by `tracks.play_count` / `.rating` /
   `.starred`. With sync no longer writing them, the UI would have shown zeros
   for every track and "most played" would have been arbitrary order. Now joins
   the caller's annotations.
3. `SyncService.__init__` initially gained `user_id: int = 1`. Removed the
   default: a silent fallback would attribute one person's history to another.

### Migration notes
- `lb_recommendation_cache` is **dropped and recreated** per-user rather than
  migrated — the old row has no defensible owner, and the 24h TTL means the cost
  is one extra API call.
- `discovery_offers` is **rebuilt** (rename → create → copy → drop) because
  SQLite cannot drop the old `recording_mbid UNIQUE` constraint in place. The
  dedupe memory (`saved` / `dismissed` status) is copied across, so dismissed
  albums are not re-offered.
- Legacy columns (`tracks.play_count` etc.) and the legacy singleton tables are
  **deliberately left in place** — they are the rollback path. Cleanup is a
  separate task, gated on production running a week.
- The owner_id backfill runs **only on the adopting startup**, not every startup.
  `DELETE /users/{id}` nulls that member's `generated_playlists.owner_id` so the
  history survives without pointing at a removed account; a repeated backfill
  would hand those rows straight back to the owner on the next restart —
  resurrecting deleted history and re-arming their triggers. Pinned by
  `test_orphan_reclaim.py`.

### Removing a member
`DELETE /api/auth/users/{id}`, owner-only. Cascades their annotations, LB/Last.fm
config, CF cache and discovery offers. Their generated-playlist **history is kept**
with `owner_id` set to NULL (it is a record of what was published), and their
triggers are orphaned rather than reassigned — an orphaned trigger refuses to run,
which is the desired outcome.

### Open item — not fixed
**Navidrome passwords are stored in plaintext** in SQLite. The scheduler needs
them to run while the user is offline, so this is per-user plaintext now. Needs
its own ticket (OS keyring, env-supplied KMS secret, or `cryptography` with a key
from the environment). Do not let this scroll away.

### Verification
- 429 → 548 tests, no regressions.
- Frontend builds (`tsc && vite build`).
- Upgrade path covered by 15+ tests that run the real `init_db()` against a
  hand-built legacy schema, including idempotency on repeated startup.

### Commits
`06978a3` models · `56d307e` per-user credentials · `965e884` owner_id columns ·
`410e624` adoption migration · `d199e16` uid in JWT · `65827a1` upgrade-path fix ·
`865eca2` users service · `4544d49` per-user annotations in sync ·
`b53adb5` auth rewiring · `8994c88` required user_id · `4d9f09e` per-user mixes ·
`b2ee6bc` per-user CF cache · `bf5f3da` per-owner scheduler ·
`c0bdc75` owned triggers · `53cca67` per-user discovery ·
`ce54b1e` library annotations · `5ea1ed9` user administration

---

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

### Bug: first live run orphaned 78 playlists (2026-11-20)

The 09:00 run reported **103 playlists pruned** but only **25 `deletePlaylist`
calls** were made. 78 playlists were still live in Navidrome afterwards, with
their `generated_playlists` rows already gone — invisible to the app and to any
future prune.

Cause: the first version reconciled by `navidrome_playlist_id`. For older
entries those stored IDs no longer matched Navidrome's (Navidrome reissues
playlist IDs after a metadata reset), so the code saw "ID not in the remote
list", concluded the playlist had been deleted by hand, and dropped the row
**without ever calling delete**. The 25 that worked were the recent rows whose
IDs happened to still be valid.

Fix: **Navidrome's own playlist list is now the source of truth, matched by
name.** Every remote playlist named `<prefix> YYYY-MM-DD` past the window is
deleted by its *actual* ID, whether or not a DB row exists; DB rows are dropped
only once their playlist is confirmed gone. A `getPlaylists` failure falls back
to deleting via stored IDs (unchanged behavior, name match cleans up next run).
Name is stable across Navidrome resets; the ID is only a hint.

Tests: 248 passing. Added the regression case explicitly (stale stored id, live
playlist under a new id → must still be deleted), a remote playlist with no DB
row at all, and recent/recent-remote/other-series guards that must not fire.

---

## 2026-11-24 — Discovery Feed (recommendations you don't own)

### What it does
New **Discover** tab. It inverts the resolver: a candidate that fails to resolve
to a local song id used to be a failure, and is now the product. Top-played
library tracks become seeds, each seed is expanded through ListenBrainz Labs
`similar-recordings`, and every neighbour is triaged against the library into
three buckets — **owned** (dropped), **gap fill** (artist present, this
recording missing) and **new artist** (nothing by them in the library).

Each feed row is an artist with its best album, the seed tracks that explain
why it appeared ("because you played The White Stripes — Fell in Love With a
Girl"), and an **Add album** button that queues just that album in Lidarr.

### Design
- **Labs, not CF.** The collaborative-filtering endpoint returns recording MBIDs
  only — no artist or title — so its output can neither be triaged by artist nor
  shown in the UI without a MusicBrainz enrichment pass at 1 req/s. Labs gives
  names, a score and a natural explanation, and needs no auth.
- **Adaptive seed scanning.** Roughly half of all recording MBIDs have *no* Labs
  neighbours. A fixed seed count therefore produced a feed dominated by
  whichever two or three seeds happened to have data — the first live run showed
  20 of 25 rows explained by a single White Stripes track. `gather_labs_candidates`
  now takes a wide pool (30, max 60) and stops early once `TARGET_CANDIDATES=300`
  have accumulated from at least `MIN_LIVE_SEEDS=6` distinct seeds, capped at
  `MAX_SEED_CALLS=45`. Live effect: 8 live seeds instead of 4, 135 artists
  instead of 40.
- **`discovery_offers` is the dedupe ledger.** Unique on `(user_id, recording_mbid)`
  (was `recording_mbid` alone before multi-user, which made it impossible to offer
  a second person a recording the first had already seen);
  `upsert_offers` bumps `last_seen`/score and merges evidence instead of
  re-inserting, and never resurrects a `dismissed` or `saved` row. Skipping
  already-seen recordings is what keeps the feed from re-offering the same
  albums every refresh.
- **One row per artist in the UI**, but offers are stored per recording (capped
  at `MAX_OFFERS_PER_ARTIST=4`) so the evidence survives and repeated
  co-occurrence raises an artist's score.
- **Lidarr: `POST /api/v1/album`, not `/artist`.** Verified against 3.1.6.5079 —
  the album endpoint auto-creates a missing artist. The trap: Lidarr monitors
  the artist's **entire discography** regardless of `addOptions.monitor`, which
  only gates the artist flag. So every add is followed by a mandatory
  `PUT /api/v1/album/monitor` collapse that unmonitors the siblings. `search`
  defaults to **false** — discovery adds a silent wishlist entry, it does not
  grab.
- **Re-adding is a no-op.** `find_album_by_release_group` runs first, so an
  already-present album returns `already_present` instead of a 400.

### Verified against the real library
- Cold run: 30 seeds scanned, 18 API calls, 306 candidates, 213 offers.
- Warm run: 4 API calls (7d `lb_similar_cache`), 71 new offers, 135 artists.
- **0 owned tracks leaked into the feed** (asserted directly against
  `tracks.recording_mbid`, not eyeballed).

### Tests
290 passing (39 new). Coverage: artist-name folding, seed selection
(one-per-artist, play-count order, MBID required), candidate dedupe and score
merging, all three triage buckets including the case where an unknown MBID on a
known artist must be a *gap fill* and not "owned", adaptive early-stop vs
`max_calls`, upsert never resurrecting dismissed rows, feed grouping/ranking,
and the Lidarr payload + monitoring collapse.

### Known gaps
- CF recommendations are not consumed yet (see above — needs MBID→artist
  enrichment).
- No scheduled refresh. The feed is refreshed on demand from the UI button.
- `live_seeds` is reported in the run summary but not persisted to
  `discovery_state`.

---

## 2026-11-24 — Lidarr add fixes (503, mis-pick, monitoring race)

The discovery feed exposed three separate defects in the album-add path. All
three were reproduced against the live Lidarr 3.1.6.5079 before being fixed.

### 1. `503 Service Unavailable` on album lookup — transient throttling
**Symptom:** adding Ellie Goulding / *Halcyon* failed with a raw
`403`-style error page worth of detail:
`Server error '503 Service Unavailable' ... /api/v1/album/lookup?term=Ellie%20Goulding%20Halcyon`.

**Cause:** Lidarr proxies MusicBrainz through its own rate-limited metadata
proxy (~1 req/s). Firing several adds in a row saturates it and it answers 503.
The identical request six seconds later returned 10 results — so the request was
never wrong. Not our bug, but our job to absorb.

**Fix:** `_request` now retries `{429, 502, 503, 504}` with backoff
`1s / 3s / 6s` plus jitter (~10s total) before failing. POST is retried too —
Lidarr keys adds on the MusicBrainz id and short-circuits when the entity
already exists. Client errors (400/404) are never retried. Confirmed live:
`Lidarr: GET album/lookup -> 503, retrying in 1.2s (attempt 1/4)` followed by
success.

### 2. Wrong album picked — lookup matched on title only
**Symptom:** the Blur offer created an artist called **sevenism** with **607
albums**. A later run picked *"Essential Blur"* (a compilation) instead of the
1997 album *"Blur"*.

**Cause:** `_pick_album` compared titles and ignored the artist entirely.
`album/lookup` is a fuzzy MusicBrainz search that happily returns other artists'
releases — `"Blur Blur"` yields "Essential Blur" (Blur), "Massive Blur"
(Melissa Ferrick), "Blur Licker" (Flex Blur) — and the cheap "title contains
query" fallback then matched an unrelated artist's release.

**Fix:** two parts.
- `_pick_album` now takes the artist name and prefers artist+exact title →
  artist+fuzzy title → any release by that artist → *then* title-only. A
  different artist's album can no longer beat the requested artist.
- New `lookup_album_for` tries `"artist album"` first and, absent an exact
  artist+title match, retries with the album name alone. The combined query
  never returns the self-titled *Blur* album; the album-only query does.

### 3. Whole discographies silently monitored — async refresh race
**Symptom:** after adding one album, the artist ended up with *all* albums
monitored. Verified live on Ellie Goulding: the collapse un-monitored 5 siblings
and reported success, then 3 were monitored again moments later.

**Cause:** `POST /api/v1/album` queues a Lidarr `RefreshArtist` command that
keeps creating the discography in the background. Each album it creates is
monitored because a new artist defaults to `monitorNewItems: "all"` — which
`addOptions.monitor: "none"` does not change (that only gates the artist flag).

**Fix:** set `monitorNewItems: "none"` on the artist at creation *and* make
`collapse_artist_monitoring` converge: re-read, re-apply, verify, for up to 4
attempts. A single fire-and-forget PUT is not sufficient.
`add_single_album` now re-reads `artist_monitored_albums` and returns
`monitoring_ok` rather than trusting its own bookkeeping.

**Related gap found while fixing this:** the "already in Lidarr" path returned
early without monitoring anything, so pressing Add on an album Lidarr already
knew about silently did nothing. It now monitors that album and collapses the
artist too.

### Also fixed: contradictory UI banners
The success toast was not cleared when a new action started, so a stale
"Passenger added" message sat next to a fresh Ellie Goulding error. All three
handlers now clear both banners first.

### Data repair
The bugs left real damage in Lidarr, which was repaired:
`monitorNewItems` set to `none` and exactly the intended album monitored for all
10 discovery-created artists (474–480, 482–484), and the wrong "Essential Blur"
album entry removed (no files were ever downloaded).

Left for the user to decide: artist **481 "sevenism"** — created by the
title-only mis-pick, 607 albums, 0 monitored, 0 files. Unmonitored so it cannot
download, but it is clutter and should probably be deleted.

### Tests
308 passing (17 new). Retry: 503/429/502 retried, 400/404 not, backoff bounded.
Pick: artist beats title-only, never selects a different artist, exact
artist+title wins, title fallback only when artist absent. Lookup: falls back to
album-name query, first query wins when exact. Collapse: converges when a
refresh re-monitors, reports non-convergence instead of claiming success.
Already-present: album actually gets monitored.

## 2026-11-26 — Curation (diversity, aging) + local-only Library Mixes

### Why
Aurral 3.0's Discover / New to You / Deep Cuts are built from the **Deezer
catalog** and filtered with `withoutOwned()` — by construction they are the
music you *don't* own, so they can never be played from Navidrome. What was
worth taking from that codebase is the machinery underneath, plus its second,
library-resident system (`mixes.ts`): Daily Mix, On Repeat, Rewind, Rediscover,
Unplayed.

### What changed
- **`services/curation.py`** (new, pure): `seeded_rng`, `weighted_sample`
  (Efraimidis–Spirakis), `familiarity_weight`, `aging_penalty`, `diversify`
  (greedy artist/genre penalties), `normalize_scores`, `apply_artist_cap`,
  `interleave`, `MIN_PLAYLIST_TRACKS`.
- **`services/mixes.py`** (new, DB read only): Deep Cuts, On Repeat, Rewind,
  Rediscover, Unplayed. All computed from `tracks` + play counts + last-played.
- **`tracks.last_played_at`** — captured from Navidrome's `played` field at sync
  time (it is returned on played songs only; the column stays NULL until a
  resync). New `smart_triggers.mix_kind` selects which mix a trigger builds.
  Both migrated in `init_db()`, following the `recording_mbid` pattern.
- **Recency trigger** now seeds its RNG from `(trigger.id, playlist_name)` and
  runs `_curate` between blend and publish.
- **`_publish_playlist`** extracted — the delete-then-create dance and the
  pruning step existed in three places; scheduled triggers now share it.
- **Publish gate**: fewer than 10 tracks and the previous playlist is left
  untouched instead of being replaced with a stub.
- Trigger type `library_mix` (default 06:30 daily, or a custom cron), with a
  mix picker in AutomationHub.

### Two bugs the tests caught
1. **Aging ran backwards.** First implementation grew the penalty with age
   (`days × 0.45`). It is a *repeat suppressant*: yesterday must cost the most,
   decaying to zero at 30 days. Fixed to `cap × (1 - days/horizon)`.
2. **The penalty was applied on the wrong scale.** `_curate` subtracted it from
   raw blend positions while `normalize_scores` then discarded magnitudes, so a
   3-point penalty only moved a track ~3 ranks and yesterday's playlist led
   again. Now scores are normalised first, then penalised, so the aging penalty
   and the diversity penalties share one 0–10 scale.

Also bounded the genre penalty to a 3-track window: cumulative genre penalties
would have banned a dominant tag ("Rock" is a third of this library) after ~10
tracks rather than interleaving it. The artist penalty stays cumulative but caps
at 3 repeats.

### Verification (real data, not mocks)
- Full sync: 878 artists / 3,127 albums / 30,356 tracks; 599 tracks got a
  `last_played_at`.
- Dry-run against a copy of the live DB: Deep Cuts 30 tracks / 10 artists,
  Unplayed 50 / 35, On Repeat 6, Rewind 4, Rediscover 0 (no play history older
  than a year yet — correct, and it says so in the log).
- End-to-end: a throwaway `library_mix` trigger published a 30-track playlist to
  Navidrome, verified the entry list, then removed both playlist and row.
- 387 tests passing (79 new across `test_curation.py`, `test_mixes.py`,
  `test_curation_pipeline.py`).

### Known limitation
On Repeat / Rewind / Rediscover stay empty until `last_played_at` is populated
by a sync, and they will not clear the 10-track publish gate on a library with
thin play counts (599 of 30k tracks here). Deep Cuts and Unplayed carry the
feature today.
