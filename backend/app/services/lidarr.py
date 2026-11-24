"""Lidarr service for interacting with Lidarr API."""
import httpx
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class LidarrService:
    """Service for interacting with Lidarr API v3."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> Any:
        """Make a request to the Lidarr API."""
        url = f"{self.base_url}/api/v1/{endpoint}"
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method, url, headers=self.headers, timeout=30.0, **kwargs
            )
            response.raise_for_status()
            # Some endpoints return empty body on success (e.g. DELETE)
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

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
    def _norm(text: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

    def _pick_album(self, results: List[Dict[str, Any]],
                    release_name: str) -> Optional[Dict[str, Any]]:
        """Pick the lookup row matching the release name, else the first."""
        if not results:
            return None
        wanted = self._norm(release_name)
        for row in results:
            if self._norm(row.get("title")) == wanted:
                return row
        # Fall back to a containment match before giving up on ordering.
        for row in results:
            title = self._norm(row.get("title"))
            if title and wanted and (title in wanted or wanted in title):
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
                                         keep_album_id: int) -> List[int]:
        """Un-monitor every album under an artist except the one we want.

        Returns the album ids that were turned off.
        """
        rows = await self._request("GET", "album", params={"artistId": artist_id})
        if not isinstance(rows, list):
            return []
        extra = [r["id"] for r in rows
                 if r.get("monitored") and r.get("id") != keep_album_id]
        if not extra:
            return []
        await self._request("PUT", "album/monitor",
                            json={"albumIds": extra, "monitored": False})
        logger.info(
            "Lidarr: collapsed artist %s monitoring to album %s (unset %d)",
            artist_id, keep_album_id, len(extra))
        return extra

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
        results = await self.lookup_album(f"{artist_name} {release_name}")
        album = self._pick_album(results if isinstance(results, list) else [],
                                 release_name)
        if album is None:
            raise ValueError(
                f"Lidarr lookup found no album for '{artist_name} - {release_name}'")

        release_group_id = album.get("foreignAlbumId")
        if not release_group_id:
            raise ValueError("Lidarr lookup returned no foreignAlbumId")

        existing = await self.find_album_by_release_group(release_group_id)
        if existing is not None:
            logger.info("Lidarr: '%s' already in library (album %s)",
                        album.get("title"), existing.get("id"))
            return {
                "album_id": existing.get("id"),
                "artist_id": existing.get("artistId"),
                "artist_name": (existing.get("artist") or {}).get("artistName")
                               or artist_name,
                "release_name": existing.get("title"),
                "already_present": True,
                "albums_unmonitored": [],
            }

        artist = dict(album.get("artist") or {})
        artist.update({
            "qualityProfileId": defaults["quality_profile_id"],
            "metadataProfileId": defaults["metadata_profile_id"],
            "rootFolderPath": defaults["root_folder"],
            "monitored": False,
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

        unmonitored = await self.collapse_artist_monitoring(artist_id, album_id)

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
        }

    async def search_album(self, album_id: int) -> Dict[str, Any]:
        """Trigger a Lidarr search for one album only."""
        return await self._request(
            "POST", "command", json={"name": "AlbumSearch", "albumIds": [album_id]})

    async def test_connection(self) -> Dict[str, Any]:
        """Test connection to Lidarr instance."""
        return await self._request("GET", "system/status")
