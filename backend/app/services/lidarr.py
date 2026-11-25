"""Lidarr service for interacting with Lidarr API."""
import asyncio
import httpx
import logging
import random
from typing import Dict, List, Any, Optional

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
            # 3. any release by the artist beats another artist's exact title
            for row in results:
                if self._norm(self._row_artist(row)) == wanted_artist:
                    return row

        # 4. No artist signal at all: fall back to title matching.
        for row in results:
            if self._norm(row.get("title")) == wanted_title:
                return row
        for row in results:
            title = self._norm(row.get("title"))
            if title and wanted_title and (title in wanted_title
                                           or wanted_title in title):
                return row
        return results[0]

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

    async def collapse_artist_monitoring(self, artist_id: int,
                                         keep_album_id: int,
                                         attempts: int = 4,
                                         delay: float = 1.5) -> List[int]:
        """Un-monitor every album under an artist except the one we want.

        Returns the album ids that were turned off.

        This has to *converge*, not fire once. `POST /album` queues a Lidarr
        `RefreshArtist` command that keeps creating the discography in the
        background; each album it creates is monitored because the artist
        defaults to `monitorNewItems: "all"`. Verified live: a single collapse
        un-monitored 5 siblings and reported success, but 3 were monitored again
        moments later. Callers should also set `monitorNewItems: "none"` on the
        artist, which stops the refresh from re-monitoring at all.
        """
        unset: set[int] = set()
        remaining: list[int] = []
        for attempt in range(attempts):
            rows = await self._request("GET", "album", params={"artistId": artist_id})
            if not isinstance(rows, list):
                return sorted(unset)
            extra = [r["id"] for r in rows
                     if r.get("monitored") and r.get("id") != keep_album_id]
            if not extra:
                if unset:
                    logger.info(
                        "Lidarr: artist %s monitoring converged on album %s "
                        "after %d attempt(s) (unset %d)",
                        artist_id, keep_album_id, attempt + 1, len(unset))
                return sorted(unset)
            await self._request("PUT", "album/monitor",
                                json={"albumIds": extra, "monitored": False})
            unset.update(extra)
            remaining = extra
            if attempt < attempts - 1:
                await asyncio.sleep(delay)

        logger.warning(
            "Lidarr: artist %s still has %s monitored besides album %s after "
            "%d attempts", artist_id, remaining, keep_album_id, attempts)
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
        artist = await self._request("GET", f"artist/{artist_id}")
        if artist.get("monitorNewItems") != "none":
            artist["monitorNewItems"] = "none"
            await self._request("PUT", f"artist/{artist_id}", json=artist)
        await self._request("PUT", "album/monitor",
                            json={"albumIds": [album_id], "monitored": True})
        return await self.collapse_artist_monitoring(artist_id, album_id)

    async def add_single_album(self, artist_name: str, release_name: str,
                               search: bool = False) -> Dict[str, Any]:
        """Add ONE album to Lidarr's wanted list without subscribing to the artist.

        Steps: lookup release group -> inject profile fields the lookup omits ->
        POST album (creates artist) -> collapse monitoring to just this album ->
        optionally trigger a search for this album alone.

        `search=False` leaves a silent wishlist entry; nothing is downloaded.
        """
        if not artist_name or not release_name:
            raise ValueError(
                "cannot add an album without both an artist and a release name")

        defaults = await self.get_album_defaults()
        results = await self.lookup_album_for(artist_name, release_name)
        album = self._pick_album(results if isinstance(results, list) else [],
                                 release_name, artist_name)
        if album is None:
            raise ValueError(
                f"Lidarr lookup found no album for '{artist_name} - {release_name}'")

        release_group_id = album.get("foreignAlbumId")
        if not release_group_id:
            raise ValueError("Lidarr lookup returned no foreignAlbumId")

        existing = await self.find_album_by_release_group(release_group_id)
        if existing is not None:
            # The album is already in Lidarr — but that does not mean it is
            # *wanted*. It may have been created unmonitored by an earlier
            # artist refresh. Clicking "Add" must still make it monitored and
            # collapse the artist, otherwise the button silently does nothing.
            album_id = existing.get("id")
            artist_id = existing.get("artistId")
            if album_id and artist_id:
                await self._ensure_single_album_monitored(artist_id, album_id)
            logger.info("Lidarr: '%s' already in library (album %s)",
                        album.get("title"), album_id)
            return {
                "album_id": album_id,
                "artist_id": artist_id,
                "artist_name": (existing.get("artist") or {}).get("artistName")
                               or artist_name,
                "release_name": existing.get("title"),
                "already_present": True,
                "albums_unmonitored": [],
                "monitoring_ok": (
                    await self.artist_monitored_albums(artist_id) == [album_id]
                    if artist_id and album_id else False
                ),
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

        unmonitored = await self.collapse_artist_monitoring(artist_id, album_id)

        # Trust the API, not our own bookkeeping: re-read what Lidarr actually
        # has monitored before claiming the add is clean.
        monitored_now = await self.artist_monitored_albums(artist_id)
        monitoring_ok = monitored_now == [album_id]
        if not monitoring_ok:
            logger.warning(
                "Lidarr: album %s added but monitoring is not clean; "
                "artist %s monitors %s", album_id, artist_id, monitored_now)

        if search:
            await self.search_album(album_id)

        logger.info(
            "Lidarr: added album '%s' by %s (album %s, artist %s), %d siblings un-monitored",
            album.get("title"), artist.get("artistName"), album_id, artist_id,
            len(unmonitored))

        return {
            "album_id": album_id,
            "artist_id": artist_id,
            "artist_name": (created.get("artist") or {}).get("artistName")
                           or artist.get("artistName"),
            "release_name": album.get("title"),
            "already_present": False,
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
