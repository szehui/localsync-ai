"""Navidrome Subsonic API client."""
import hashlib
import httpx
import secrets
from typing import Optional
from app.config import settings


class NavidromeClient:
    """Async client for Navidrome's Subsonic API."""

    def __init__(self, url: str = "", username: str = "", password: str = ""):
        self.url = (url or settings.navidrome_url).rstrip("/")
        self.username = username or settings.navidrome_username
        self.password = password or settings.navidrome_password
        self._client = httpx.AsyncClient(timeout=30.0)

    def _auth_params(self) -> dict:
        """Generate Subsonic auth parameters (salt + token)."""
        salt = secrets.token_hex(16)
        token = hashlib.md5(f"{self.password}{salt}".encode()).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",  # Subsonic API version
            "c": "localsync-ai",
            "f": "json",
        }

    async def _request(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a Subsonic API request."""
        url = f"{self.url}/rest/{endpoint}"
        all_params = {**self._auth_params(), **(params or {})}
        response = await self._client.get(url, params=all_params)
        response.raise_for_status()
        data = response.json()
        subsonic = data.get("subsonic-response", {})
        if subsonic.get("status") != "ok":
            error = subsonic.get("error", {})
            raise Exception(f"Subsonic error: {error.get('message', 'Unknown')}")
        return subsonic

    async def ping(self) -> dict:
        """Test connectivity. Returns server version info."""
        return await self._request("ping.view")

    async def get_artists(self) -> list[dict]:
        """Get all artists (ID3 tags)."""
        data = await self._request("getArtists.view")
        indexes = data.get("artists", {}).get("index", [])
        artists = []
        for index in indexes:
            for artist in index.get("artist", []):
                artists.append(artist)
        return artists

    async def get_artist(self, artist_id: str) -> dict:
        """Get artist details with albums."""
        return await self._request("getArtist.view", {"id": artist_id})

    async def get_album(self, album_id: str) -> dict:
        """Get album details with tracks."""
        return await self._request("getAlbum.view", {"id": album_id})

    async def get_album_list2(self, type_: str = "newest", size: int = 500, offset: int = 0) -> list[dict]:
        """Get album list by type (newest, frequent, recent, random, etc.)."""
        data = await self._request("getAlbumList2.view", {
            "type": type_,
            "size": size,
            "offset": offset,
        })
        return data.get("albumList2", {}).get("album", [])

    async def get_song(self, song_id: str) -> dict:
        """Get single song details."""
        data = await self._request("getSong.view", {"id": song_id})
        return data.get("song", {})

    async def get_similar_songs2(self, song_id: str, count: int = 50) -> list[dict]:
        """Get similar songs from the library."""
        data = await self._request("getSimilarSongs2.view", {
            "id": song_id,
            "count": count,
        })
        return data.get("similarSongs2", {}).get("song", [])

    async def get_similar_artists(self, artist_id: str, count: int = 20) -> list[dict]:
        """Get similar artists."""
        data = await self._request("getSimilarArtists.view", {
            "id": artist_id,
            "count": count,
        })
        return data.get("similarArtists", {}).get("artist", [])

    async def get_top_songs(self, artist_name: str, count: int = 50) -> list[dict]:
        """Get top songs for an artist."""
        data = await self._request("getTopSongs.view", {
            "artist": artist_name,
            "count": count,
        })
        return data.get("topSongs", {}).get("song", [])

    async def search(self, query: str, artist_count: int = 20, album_count: int = 20, song_count: int = 50) -> dict:
        """Search across the library."""
        return await self._request("search3.view", {
            "query": query,
            "artistCount": artist_count,
            "albumCount": album_count,
            "songCount": song_count,
        })

    async def create_playlist(self, name: str, track_ids: list[str]) -> dict:
        """Create a new playlist on Navidrome."""
        params = {"name": name}
        # Subsonic API expects multiple songId params, not indexed songId0/songId1
        if track_ids:
            params["songId"] = track_ids
        return await self._request("createPlaylist.view", params)

    async def update_playlist(self, playlist_id: str, track_ids: list[str], name: str | None = None) -> dict:
        """Update an existing playlist (replace all tracks)."""
        params = {"playlistId": playlist_id}
        if name:
            params["name"] = name
        if track_ids:
            params["songId"] = track_ids
        return await self._request("updatePlaylist.view", params)

    async def get_playlists(self) -> list[dict]:
        """Get all playlists."""
        data = await self._request("getPlaylists.view")
        return data.get("playlists", {}).get("playlist", [])

    async def get_playlist(self, playlist_id: str) -> dict:
        """Get playlist details with tracks."""
        return await self._request("getPlaylist.view", {"id": playlist_id})

    async def close(self):
        await self._client.aclose()
