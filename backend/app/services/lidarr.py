"""Lidarr service for interacting with Lidarr API."""
import httpx
import logging
from typing import Dict, List, Any

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

    async def test_connection(self) -> Dict[str, Any]:
        """Test connection to Lidarr instance."""
        return await self._request("GET", "system/status")
