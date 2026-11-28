"""Lidarr service for interacting with Lidarr API."""
import asyncio
import httpx
import logging
import random
from typing import Dict, Iterable, List, Any, Optional

logger = logging.getLogger(__name__)

# Lidarr fronts MusicBrainz through its own metadata proxy, which is rate limited
# to ~1 request/second. When it is saturated it answers **503 Service
# Unavailable** — verified live: the same `album/lookup` that returned 503
# succeeded 6s later with 10 results. This is transient throttling, not a bad
# request, so it must be retried rather than surfaced to the user.
#
# Clicking "Add album" a few times in a row is enough to trip it, which is
# exactly how the feed gets used.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_BACKOFF = (1.0, 3.0, 6.0)  # total added latency ~10s before giving up


class LidarrService:
    """Service for interacting with Lidarr API v3."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> Any:
        """Make a request to the Lidarr API, retrying transient throttling.

        POST is retried too: Lidarr's album/artist adds are keyed on the
        MusicBrainz id and short-circuit when the entity already exists, so a
        retry cannot duplicate a library entry.
        """
        url = f"{self.base_url}/api/v1/{endpoint}"
        attempts = len(RETRY_BACKOFF) + 1
        async with httpx.AsyncClient() as client:
            for attempt in range(attempts):
                response = await client.request(
                    method, url, headers=self.headers, timeout=30.0, **kwargs
                )
                if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
                    delay = RETRY_BACKOFF[attempt] + random.uniform(0, 0.5)
                    logger.warning(
                        "Lidarr: %s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                        method, endpoint, response.status_code, delay,
                        attempt + 1, attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                # Some endpoints return empty body on success (e.g. DELETE)
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
        raise RuntimeError(f"unreachable: {method} {endpoint}")  # pragma: no cover

    async def get_root_folder(self) -> Dict[str, Any]:
        """Get the first configured root folder from Lidarr."""
        data = await self._request("GET", "rootfolder")
        if not data:
            raise ValueError("No root folders configured in Lidarr")
        return data[0]

    async def lookup_artist(self, term: str) -> List[Dict[str, Any]]:
        """Lookup an artist by name. Returns list of candidate artists."""
        return await self._request("GET", "artist/lookup", params={"term": term})

    async def get_artists(self) -> List[Dict[str, Any]]:
        """Get all artists currently monitored by Lidarr."""
        return await self._request("GET", "artist")

    async def get_all_albums(self) -> List[Dict[str, Any]]:
        """Every album row Lidarr holds, with its resolved `foreignAlbumId`.

        Used as an MBID oracle: Lidarr already resolved a release group for each
        album it knows, so its id can be copied onto the matching library row
        instead of re-deriving it from MusicBrainz (see services/mbid_backfill).
        """
        return await self._request("GET", "album")

    async def add_artist(
        self, artist_data: Dict[str, Any], root_folder: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add an artist to Lidarr for monitoring and searching.

        The artist/lookup endpoint returns partial objects. We build a minimal
        add payload using only the fields the POST /api/v1/artist endpoint
        actually requires:
          - foreignArtistId  (MusicBrainz ID from lookup)
          - artistName      (display name from lookup)
          - qualityProfileId (from root folder defaults)
          - metadataProfileId (from root folder defaults)
          - rootFolderPath  (from root folder)
          - monitored: true
          - addOptions.monitor: "all"
          - addOptions.searchForMissingAlbums: true
        """
        foreign_id = artist_data.get("foreignArtistId")
        artist_name = artist_data.get("artistName") or artist_data.get("name", "Unknown")

        if not foreign_id:
            raise ValueError(
                f"Cannot add artist '{artist_name}': no foreignArtistId "
                "(MusicBrainz ID) in lookup result"
            )

        payload = {
            "artistName": artist_name,
            "foreignArtistId": foreign_id,
            "qualityProfileId": root_folder["defaultQualityProfileId"],
            "metadataProfileId": root_folder["defaultMetadataProfileId"],
            "rootFolderPath": root_folder["path"],
            "monitored": True,
            "addOptions": {
                "monitor": "all",
                "searchForMissingAlbums": True,
            },
        }

        logger.info(f"Adding artist to Lidarr: {artist_name} ({foreign_id[:8]}...)")
        return await self._request("POST", "artist", json=payload)

    # ── album-level adds (verified against Lidarr 3.1.6.5079) ────────────────
    #
    # `POST /api/v1/album` creates the ARTIST too when it is missing, but it
    # monitors the artist's ENTIRE discography regardless of the artist's
    # `addOptions.monitor` setting. From AddArtistService:
    #
    #     if (newArtist.AddOptions.Monitor == MonitorTypes.None)
    #         newArtist.Monitored = false;    // the ARTIST flag only
    #
    # ...and then AddAlbumService triggers an album refresh which brings the
    # whole discography in monitored. Observed live: 7/7 Strokes albums monitored
    # with the artist itself unmonitored. So collapsing by name is mandatory,
    # not cosmetic.

    @staticmethod
    def _norm(text: Optional[str]) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

    @staticmethod
    def _row_artist(row: Dict[str, Any]) -> str:
        artist = row.get("artist") or {}
        return artist.get("artistName") or row.get("artistName") or ""

    def _pick_album(self, results: List[Dict[str, Any]],
                    release_name: str,
                    artist_name: str = "") -> Optional[Dict[str, Any]]:
        """Pick the lookup row for this artist+release.

        Artist must be considered, not just the title. `album/lookup` returns
        other artists' releases whose *titles* contain the query — searching
        "Blur Blur" yields "Essential Blur" (Blur), "Massive Blur" (Melissa
        Ferrick) and so on. Title-only matching picked "Essential Blur" for the
        album "Blur", and in one live run selected a release by a completely
        unrelated artist. So: prefer artist+title, then artist, and only fall
        back to a title-only guess when the artist appears nowhere.
        """
        if not results:
            return None
        wanted_title = self._norm(release_name)
        wanted_artist = self._norm(artist_name)

        if wanted_artist:
            # 1. artist + exact title
            for row in results:
                if (self._norm(self._row_artist(row)) == wanted_artist
                        and self._norm(row.get("title")) == wanted_title):
                    return row
            # 2. artist + fuzzy title
            for row in results:
                if self._norm(self._row_artist(row)) != wanted_artist:
                    continue
                title = self._norm(row.get("title"))
                if title and wanted_title and (title in wanted_title
                                               or wanted_title in title):
                    return row
            # 3. Artist matches but the title doesn't: only accept a genuinely
            #    similar title. "Any release by this artist" is how the Shang-Chi
            #    soundtrack resolved to Savage Mode — an album the user never
            #    asked for, queued for download under the right artist's name.
            for row in results:
                if self._norm(self._row_artist(row)) != wanted_artist:
                    continue
                if self._title_agrees(row.get("title"), release_name):
                    return row

        # 4. Title evidence plus a *similar* artist name. MusicBrainz spells acts
        #    differently ("BTS (방탄소년단)" vs "BTS"), so an exact fold is too
        #    strict here — but a completely different artist must never win on
        #    title alone, which is how a stranger's album gets downloaded. When
        #    no artist was supplied at all, the title is all the evidence there is.
        for row in results:
            if not self._title_agrees(row.get("title"), release_name):
                continue
            if not artist_name or self._artist_agrees(
                    self._row_artist(row), artist_name):
                return row
        # Nothing matched on evidence. Returning a best-guess here means adding
        # an arbitrary album; the caller is better off treating it as unattributed.
        return None

    @staticmethod
    def _artist_agrees(candidate: Optional[str], wanted: str) -> bool:
        """True if a candidate artist name plausibly means the wanted artist."""
        import difflib
        import re

        def fold(text: Optional[str]) -> str:
            return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

        a, b = fold(candidate), fold(wanted)
        if not a or not b:
            return False
        if a == b:
            return True
        if (len(b) >= 3 and (a in b or b in a)):
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7

    @staticmethod
    def _title_agrees(candidate: Optional[str], wanted: str) -> bool:
        """True if two release titles are the same after folding, or close.

        Substring containment alone is not enough ("Blur" is inside
        "Blurred Lines"), so a similarity floor backs it up.
        """
        import difflib
        import re
        a = re.sub(r"[^a-z0-9]+", "", (candidate or "").lower())
        b = re.sub(r"[^a-z0-9]+", "", (wanted or "").lower())
        if not a or not b:
            return False
        if a == b:
            return True
        if len(b) >= 6 and (a in b or b in a):
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75

    async def get_album_defaults(self) -> Dict[str, Any]:
        """Quality profile / metadata profile / root folder for a new artist."""
        quality = await self._request("GET", "qualityprofile")
        metadata = await self._request("GET", "metadataprofile")
        roots = await self._request("GET", "rootfolder")
        if not quality or not metadata or not roots:
            raise ValueError(
                "Lidarr has no quality profile, metadata profile or root folder "
                "configured; cannot add an album"
            )
        return {
            "quality_profile_id": quality[0]["id"],
            "metadata_profile_id": metadata[0]["id"],
            "root_folder": roots[0]["path"],
        }

    async def lookup_album_by_mbid(self, mbid: str) -> Optional[Dict[str, Any]]:
        """Exact release-group lookup. Returns None rather than a best guess.

        `album/lookup?term=mbid:<uuid>` is the only lookup form that cannot
        pick the wrong artist, and it returns exactly one row — but the caller
        still verifies `foreignAlbumId`, because "one row" is an observed
        property of this Lidarr version, not a documented contract, and adding
        the wrong album is unrecoverable from the user's point of view.
        """
        if not mbid:
            return None
        rows = await self.lookup_album(f"mbid:{mbid}")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if (row.get("foreignAlbumId") or "").lower() == mbid.lower():
                return row
        if rows:
            logger.warning(
                "Lidarr: lookup for mbid:%s returned %d row(s) with a different "
                "foreignAlbumId (%s) — refusing to use them",
                mbid, len(rows), rows[0].get("foreignAlbumId"),
            )
        return None

    def _exact_album(self, results: List[Dict[str, Any]], release_name: str,
                     artist_name: str) -> Optional[Dict[str, Any]]:
        """Row whose artist AND title both match exactly (after folding)."""
        if not artist_name:
            return None
        wanted_artist = self._norm(artist_name)
        wanted_title = self._norm(release_name)
        for row in results:
            if (self._norm(self._row_artist(row)) == wanted_artist
                    and self._norm(row.get("title")) == wanted_title):
                return row
        return None

    async def lookup_album_for(self, artist_name: str,
                               release_name: str) -> List[Dict[str, Any]]:
        """Look an album up, returning the row we should actually use.

        One query is not enough. Lidarr's `album/lookup` is a fuzzy MusicBrainz
        search that returns ~10 rows, and combining artist+album often ranks
        unrelated titles above the real one: `"Blur Blur"` returns "Essential
        Blur", "Blur Licker" and "Massive Blur" but *never* the 1997 album
        "Blur" — while searching `"Blur"` alone does contain it. So: try the
        combined term, and if that yields no exact artist+title match, retry
        with the album name only before settling for a fuzzy match.

        Returns a single-element list (or empty) so it can drop into the same
        `_pick_album` flow.
        """
        queries = [f"{artist_name} {release_name}", release_name]
        collected: List[List[Dict[str, Any]]] = []
        for query in queries:
            if not query.strip():
                continue
            try:
                rows = await self.lookup_album(query)
            except httpx.HTTPError:
                logger.warning("Lidarr: album lookup failed for %r", query)
                continue
            if not isinstance(rows, list) or not rows:
                continue
            exact = self._exact_album(rows, release_name, artist_name)
            if exact is not None:
                return [exact]
            collected.append(rows)

        # Nothing exact: prefer a fuzzy pick whose artist is still correct.
        wanted_artist = self._norm(artist_name)
        for rows in collected:
            pick = self._pick_album(rows, release_name, artist_name)
            if pick is not None and wanted_artist and \
                    self._norm(self._row_artist(pick)) == wanted_artist:
                return [pick]
        if collected:
            pick = self._pick_album(collected[0], release_name, artist_name)
            return [pick] if pick is not None else []
        return []

    async def lookup_album(self, term: str) -> List[Dict[str, Any]]:
        """Search MusicBrainz release groups through Lidarr."""
        return await self._request("GET", "album/lookup", params={"term": term})

    async def find_album_by_release_group(
        self, foreign_album_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the album already in Lidarr for a release-group MBID, if any."""
        rows = await self._request(
            "GET", "album", params={"foreignAlbumId": foreign_album_id})
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    # ── Status truth ────────────────────────────────────────────────────────
    #
    # The reconciler needs the queue and history, and it needs each exactly
    # once per cycle rather than once per open request: a household with ten
    # pending requests would otherwise pull the same 42-record queue ten times.

    async def get_queue(self, page_size: int = 200, max_pages: int = 5) -> List[Dict[str, Any]]:
        """Every record currently in Lidarr's download queue.

        `status` is the coarse state (downloading / delay / queued) and
        `trackedDownloadState` is the importer's view, which is null for
        records that have not reached the importer yet — so the reconciler
        treats presence in the queue as "downloading" and lets album
        statistics decide when files have actually landed.
        """
        records: List[Dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            body = await self._request(
                "GET", "queue",
                params={"page": page, "pageSize": page_size,
                        "includeUnknownArtistItems": "true"},
            )
            batch = (body or {}).get("records") or []
            records.extend(batch)
            total = (body or {}).get("totalRecords")
            # Stop on a short page, or when the server says we have it all.
            if len(batch) < page_size or (total is not None and len(records) >= total):
                break
            page += 1
        return records

    async def get_history(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """The most recent history events, newest first.

        Deliberately not paginated deep: the reconciler only cares about
        failures newer than a request's `requested_at`, and those are recent
        by definition. A 43k-record history would otherwise be walked every
        five minutes to answer a question about the last few hours.
        """
        body = await self._request(
            "GET", "history",
            params={"page": 1, "pageSize": page_size,
                    "sortKey": "date", "sortDirection": "descending"},
        )
        return (body or {}).get("records") or []

    async def delete_album(self, album_id: int, delete_files: bool = False) -> Any:
        """Remove an album entry. `delete_files=False` leaves downloads alone."""
        return await self._request(
            "DELETE", f"album/{album_id}",
            params={"deleteFiles": str(delete_files).lower()},
        )

    async def collapse_artist_monitoring(self, artist_id: int,
                                         keep_album_id: Optional[int] = None,
                                         attempts: int = 4,
                                         delay: float = 1.5,
                                         *,
                                         keep_album_ids: Optional[Iterable[int]] = None,
                                         ) -> List[int]:
        """Un-monitor every album under an artist except the ones we want.

        Pass a single album id (`keep_album_id`) for the one-album case, or a
        set (`keep_album_ids`) when a batch add legitimately wants several
        releases by the same artist — otherwise each add in the loop would
        un-monitor the one before it.

        Returns the album ids that were turned off.

        This has to *converge*, not fire once. `POST /album` queues a Lidarr
        `RefreshArtist` command that keeps creating the discography in the
        background; each album it creates is monitored because the artist
        defaults to `monitorNewItems: "all"`. Verified live: a single collapse
        un-monitored 5 siblings and reported success, but 3 were monitored again
        moments later. Callers should also set `monitorNewItems: "none"` on the
        artist, which stops the refresh from re-monitoring at all.
        """
        if keep_album_ids is not None:
            keep = set(keep_album_ids)
        elif keep_album_id is not None:
            keep = {keep_album_id}
        else:
            raise ValueError("keep_album_id or keep_album_ids is required")

        unset: set[int] = set()
        remaining: list[int] = []
        for attempt in range(attempts):
            rows = await self._request("GET", "album", params={"artistId": artist_id})
            if not isinstance(rows, list):
                return sorted(unset)
            extra = [r["id"] for r in rows
                     if r.get("monitored") and r.get("id") not in keep]
            if not extra:
                if unset:
                    logger.info(
                        "Lidarr: artist %s monitoring converged on %s after "
                        "%d attempt(s) (unset %d)",
                        artist_id, sorted(keep), attempt + 1, len(unset))
                return sorted(unset)
            await self._request("PUT", "album/monitor",
                                json={"albumIds": extra, "monitored": False})
            unset.update(extra)
            remaining = extra
            if attempt < attempts - 1:
                await asyncio.sleep(delay)

        logger.warning(
            "Lidarr: artist %s still has %s monitored besides %s after "
            "%d attempts", artist_id, remaining, sorted(keep), attempts)
        return sorted(unset)

    async def artist_monitored_albums(self, artist_id: int) -> List[int]:
        """Album ids currently monitored under an artist."""
        rows = await self._request("GET", "album", params={"artistId": artist_id})
        if not isinstance(rows, list):
            return []
        return [r["id"] for r in rows if r.get("monitored")]

    async def _ensure_single_album_monitored(self, artist_id: int,
                                             album_id: int) -> List[int]:
        """Monitor exactly one album under an artist; unmonitor the rest."""
        return await self._monitor_only(artist_id, [album_id])

    async def _monitor_only(self, artist_id: int,
                            keep_album_ids: Iterable[int]) -> List[int]:
        """Make `keep_album_ids` the artist's monitored set, and nothing else."""
        keep = list(keep_album_ids)
        artist = await self._request("GET", f"artist/{artist_id}")
        if artist.get("monitorNewItems") != "none":
            artist["monitorNewItems"] = "none"
            await self._request("PUT", f"artist/{artist_id}", json=artist)
        await self._request("PUT", "album/monitor",
                            json={"albumIds": keep, "monitored": True})
        return await self.collapse_artist_monitoring(
            artist_id, keep_album_ids=keep)

    async def _resolve_album(self, artist_name: str,
                             release_name: str) -> Dict[str, Any]:
        """Find the Lidarr lookup row for an artist+title pair."""
        if not artist_name or not release_name:
            raise ValueError(
                "cannot add an album without both an artist and a release name")
        results = await self.lookup_album_for(artist_name, release_name)
        album = self._pick_album(results if isinstance(results, list) else [],
                                 release_name, artist_name)
        if album is None:
            raise ValueError(
                f"Lidarr lookup found no album for '{artist_name} - {release_name}'")
        return album

    async def _resolve_album_by_mbid(self, mbid: str) -> Dict[str, Any]:
        """Find the Lidarr lookup row for a release-group MBID, or fail loudly.

        There is no fuzzy step and no substitute: the request names a specific
        release group, and adding a different one under the same artist would
        look like success while downloading something the user never asked for.
        """
        album = await self.lookup_album_by_mbid(mbid)
        if album is None:
            raise ValueError(f"Lidarr has no album for release group {mbid}")
        return album

    async def _post_album(self, album: Dict[str, Any],
                          defaults: Dict[str, Any],
                          search: bool = False) -> Dict[str, Any]:
        """POST one resolved album to Lidarr. The single add path.

        Both resolvers (title and MBID) land here, so there is exactly one
        place that builds the payload and knows the already-present semantics.
        Does NOT touch artist monitoring: batch callers collapse once per artist
        afterwards, and a per-add collapse would un-monitor the previous add
        (34 of 88 lost in a live run before this rule).
        """
        release_group_id = album.get("foreignAlbumId")
        if not release_group_id:
            raise ValueError("Lidarr lookup returned no foreignAlbumId")

        existing = await self.find_album_by_release_group(release_group_id)
        if existing is not None:
            # The album is already in Lidarr — but that does not mean it is
            # *wanted*. It may have been created unmonitored by an earlier
            # artist refresh. Clicking "Add" must still make it monitored.
            logger.info("Lidarr: '%s' already in library (album %s)",
                        album.get("title"), existing.get("id"))
            return {
                "album_id": existing.get("id"),
                "artist_id": existing.get("artistId"),
                "artist_name": (existing.get("artist") or {}).get("artistName")
                               or (album.get("artist") or {}).get("artistName"),
                "release_name": existing.get("title"),
                "already_present": True,
            }

        artist = dict(album.get("artist") or {})
        artist.update({
            "qualityProfileId": defaults["quality_profile_id"],
            "metadataProfileId": defaults["metadata_profile_id"],
            "rootFolderPath": defaults["root_folder"],
            "monitored": False,
            # Without this the artist defaults to monitorNewItems="all", so the
            # RefreshArtist command queued by the POST re-monitors every album it
            # creates — undoing the collapse below. Verified live.
            "monitorNewItems": "none",
            "addOptions": {
                "monitor": "none",
                "searchForMissingAlbums": False,
                "albumsToMonitor": [],
            },
        })

        payload = dict(album)
        payload.update({
            "monitored": True,
            "addOptions": {"addType": "manual", "searchForNewAlbum": search},
            "artist": artist,
        })

        created = await self._request("POST", "album", json=payload)
        album_id = created.get("id") if isinstance(created, dict) else None
        artist_id = created.get("artistId") if isinstance(created, dict) else None
        if not album_id:
            raise ValueError(f"Lidarr did not return an album id: {created!r}")
        if not artist_id:
            raise ValueError(f"Lidarr did not return an artist id: {created!r}")

        if search:
            await self.search_album(album_id)

        logger.info("Lidarr: added album '%s' by %s (album %s, artist %s)",
                    album.get("title"), artist.get("artistName"),
                    album_id, artist_id)

        return {
            "album_id": album_id,
            "artist_id": artist_id,
            "artist_name": (created.get("artist") or {}).get("artistName")
                           or artist.get("artistName"),
            "release_name": album.get("title"),
            "already_present": False,
        }

    async def _add_album_core(self, artist_name: str, release_name: str,
                              search: bool = False) -> Dict[str, Any]:
        """Add one album by artist+title. Does NOT touch artist monitoring.

        The title path still exists for the YouTube import, which is handed
        names rather than MBIDs. Requests do not use it: `add_albums_by_mbid`
        goes through `_resolve_album_by_mbid` instead, so the fuzzy picker can
        never choose an album on the request path.
        """
        # Order matters and both halves are pinned by tests: reject blank names
        # before touching the network, then verify Lidarr is configured, and
        # only then spend a lookup. (Resolving first made a misconfigured Lidarr
        # report a lookup failure instead of the real problem.)
        if not artist_name or not release_name:
            raise ValueError(
                "cannot add an album without both an artist and a release name")
        defaults = await self.get_album_defaults()
        album = await self._resolve_album(artist_name, release_name)
        return await self._post_album(album, defaults, search)

    async def add_albums(self, items: Iterable[tuple], search: bool = False,
                         delay: float = 1.5,
                         ) -> Dict[str, Any]:
        """Add several albums at once, collapsing monitoring ONCE per artist.

        `add_single_album` in a loop is wrong for this job: each add un-monitors
        the artist's other albums, so adding eight ENHYPEN albums leaves only
        the eighth monitored (34 of 88 in a live run). Here every artist gets a
        single collapse that keeps all of *our* albums.

        `items` is an iterable of `(artist_name, release_name)` pairs.
        `delay` seconds are inserted between adds: Lidarr fronts MusicBrainz
        through a metadata proxy rate-limited to ~1 req/s; hammering lookup/
        album endpoints back-to-back yields 503s, which the caller doesn't need
        to know about.

        Returns ``{"added", "already_present", "failed", "monitoring_ok",
        "results"}``.
        """
        results: List[Dict[str, Any]] = []
        by_artist: Dict[int, List[int]] = {}
        items = list(items)

        for index, (artist_name, release_name) in enumerate(items):
            try:
                res = await self._add_album_core(
                    artist_name, release_name, search=search)
                results.append(res)
                artist_id, album_id = res.get("artist_id"), res.get("album_id")
                if artist_id and album_id:
                    by_artist.setdefault(int(artist_id), []).append(int(album_id))
            except Exception as e:
                logger.warning("Lidarr: could not add %s - %s: %s",
                               artist_name, release_name, e)
                results.append({
                    "artist_name": artist_name,
                    "release_name": release_name,
                    "error": str(e),
                })

            if index < len(items) - 1:
                await asyncio.sleep(delay)

        monitored_ok = await self._collapse_by_artist(by_artist)

        added = [r for r in results if r.get("album_id") and not r.get("error")]
        return {
            "added": [r for r in added if not r.get("already_present")],
            "already_present": [r for r in added if r.get("already_present")],
            "failed": [r for r in results if r.get("error")],
            "monitoring_ok": monitored_ok,
            "results": results,
        }

    async def add_albums_by_mbid(self, mbids: Iterable[str],
                                 search: bool = False,
                                 delay: float = 1.0) -> Dict[str, Any]:
        """Add several release groups by MBID, collapsing monitoring once per artist.

        This is the request-approval path. Compared with `add_albums`, the
        lookup cannot be wrong: each MBID is resolved exactly and an unresolved
        id is *reported*, never substituted with a fuzzy match. A discography
        request therefore either adds the albums the user chose or says which
        ones failed and why.

        `delay` is shorter than the title path's 1.5s because each iteration is
        a single MBID lookup rather than one or two fuzzy searches.
        """
        results: List[Dict[str, Any]] = []
        by_artist: Dict[int, List[int]] = {}
        mbids = list(mbids)

        if not mbids:
            return {"added": [], "already_present": [], "unresolved": [],
                    "failed": [], "monitoring_ok": True, "results": []}

        defaults = await self.get_album_defaults()

        for index, mbid in enumerate(mbids):
            try:
                album = await self._resolve_album_by_mbid(mbid)
                res = await self._post_album(album, defaults, search=search)
                res = {**res, "release_group_mbid": mbid}
                results.append(res)
                artist_id, album_id = res.get("artist_id"), res.get("album_id")
                if artist_id and album_id:
                    by_artist.setdefault(int(artist_id), []).append(int(album_id))
            except Exception as e:
                logger.warning("Lidarr: could not add MBID %s: %s", mbid, e)
                results.append({"release_group_mbid": mbid, "error": str(e)})

            if index < len(mbids) - 1:
                await asyncio.sleep(delay)

        monitored_ok = await self._collapse_by_artist(by_artist)

        added = [r for r in results if r.get("album_id") and not r.get("error")]
        failed = [r for r in results if r.get("error")]
        return {
            "added": [r for r in added if not r.get("already_present")],
            "already_present": [r for r in added if r.get("already_present")],
            # Unresolved is distinct from failed: "Lidarr does not know this
            # release group" is a dead end the user must be told about, while a
            # transport error is worth retrying.
            "unresolved": [r for r in failed if "no album for release group" in r["error"]],
            "failed": [r for r in failed if "no album for release group" not in r["error"]],
            "monitoring_ok": monitored_ok,
            "results": results,
        }

    async def _collapse_by_artist(self, by_artist: Dict[int, List[int]]) -> bool:
        """Collapse monitoring once per artist, verifying against the API.

        Shared by the title and MBID batch paths so neither can drift into the
        per-add collapse that un-monitors its own earlier adds.
        """
        monitored_ok = True
        for artist_id, album_ids in by_artist.items():
            try:
                await self._monitor_only(artist_id, album_ids)
                # Trust the API over our bookkeeping.
                now = set(await self.artist_monitored_albums(artist_id))
                if not set(album_ids).issubset(now):
                    monitored_ok = False
                    logger.warning(
                        "Lidarr: artist %s monitors %s, expected %s",
                        artist_id, sorted(now), sorted(album_ids))
            except Exception as e:
                monitored_ok = False
                logger.warning(
                    "Lidarr: monitoring pass failed for artist %s: %s",
                    artist_id, e)
        return monitored_ok

    async def add_single_album(self, artist_name: str, release_name: str,
                               search: bool = False) -> Dict[str, Any]:
        """Add ONE album to Lidarr's wanted list without subscribing to the artist.

        Steps: lookup release group -> inject profile fields the lookup omits ->
        POST album (creates artist) -> collapse monitoring to just this album ->
        optionally trigger a search for this album alone.

        `search=False` leaves a silent wishlist entry; nothing is downloaded.

        For several albums in one go use `add_albums`, which collapses once per
        artist instead of once per album.
        """
        res = await self._add_album_core(artist_name, release_name, search=search)
        artist_id, album_id = res.get("artist_id"), res.get("album_id")

        unmonitored: List[int] = []
        monitoring_ok = False
        if artist_id and album_id:
            unmonitored = await self._monitor_only(artist_id, [album_id])
            monitored_now = await self.artist_monitored_albums(artist_id)
            monitoring_ok = monitored_now == [album_id]
            if not monitoring_ok:
                logger.warning(
                    "Lidarr: album %s added but monitoring is not clean; "
                    "artist %s monitors %s", album_id, artist_id, monitored_now)

        return {
            **res,
            "albums_unmonitored": unmonitored,
            "monitoring_ok": monitoring_ok,
        }

    async def search_album(self, album_id: int) -> Dict[str, Any]:
        """Trigger a Lidarr search for one album only."""
        return await self._request(
            "POST", "command", json={"name": "AlbumSearch", "albumIds": [album_id]})

    async def test_connection(self) -> Dict[str, Any]:
        """Test connection to Lidarr instance."""
        return await self._request("GET", "system/status")
