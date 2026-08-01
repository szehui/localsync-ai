"""Last.fm API client for fetching scrobble history and similar tracks."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0"


class LastfmClient:
    """Async client for the Last.fm API."""

    def __init__(self, api_key: str, username: str):
        self.api_key = api_key
        self.username = username
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, **params) -> dict:
        params.setdefault("format", "json")
        params.setdefault("api_key", self.api_key)
        response = await self._client.get(LASTFM_API_BASE, params={"method": method, **params})
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise Exception(f"Last.fm API error {data['error']}: {data.get('message', 'Unknown')}")
        return data

    async def get_recent_tracks(self, days: int = 7, limit: int = 200) -> list[dict]:
        """Fetch scrobbles from the last N days.

        Returns a list of dicts with keys: artist, title, album (optional), played_at (ISO str or None).
        """
        now = datetime.now(timezone.utc)
        since = int((now - timedelta(days=days)).timestamp())

        all_tracks = []
        page = 1
        total_pages = 1

        max_pages = 10  # safety cap: max 10 pages (2000 tracks)
        while page <= total_pages and page <= max_pages:
            try:
                data = await self._request(
                    "user.getRecentTracks",
                    user=self.username,
                    limit=limit,
                    page=page,
                    **{"from": since},
                )
            except Exception as e:
                logger.warning(f"Last.fm page {page} failed: {e}")
                break

            recent_tracks = data.get("recenttracks", {})
            attr = recent_tracks.get("@attr", {})
            total_pages = int(attr.get("totalPages", 1))
            tracks = recent_tracks.get("track", [])
            if not tracks:
                break

            for track in tracks:
                artist = track.get("artist", {})
                title = track.get("name", "")
                if not title or not artist.get("#text"):
                    continue
                entry = {
                    "artist": artist["#text"],
                    "title": title,
                    "album": track.get("album", {}).get("#text", ""),
                    "nowplaying": track.get("@attr", {}).get("nowplaying", "false") == "true",
                }
                # Parse played date if available
                date_info = track.get("date", {})
                if date_info and date_info.get("#text"):
                    entry["played_at"] = date_info["#text"]
                else:
                    entry["played_at"] = None

                # Skip currently-playing track (no date = nowplaying)
                if entry["nowplaying"]:
                    continue

                all_tracks.append(entry)

            page += 1

        # Deduplicate by (artist, title) — keep most recent occurrence
        seen = set()
        unique = []
        for t in reversed(all_tracks):  # reverse so first occurrence wins (most recent)
            key = (t["artist"].lower().strip(), t["title"].lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(t)
        unique.reverse()  # back to chronological

        logger.info(
            f"Last.fm: fetched {len(all_tracks)} scrobbles, "
            f"{len(unique)} unique tracks in last {days} days"
        )
        return unique

    async def get_similar(self, artist: str, track: str, limit: int = 50) -> list[dict]:
        """Get similar tracks for a given artist and track from Last.fm.

        Returns a list of Last.fm track dicts with keys:
            artist (dict with #text), name, mbid (optional), url, image, etc.
        """
        try:
            data = await self._request(
                "track.getSimilar",
                artist=artist,
                track=track,
                limit=limit,
            )
            return data.get("similartracks", {}).get("track", [])
        except Exception as e:
            logger.warning(f"Last.fm get_similar failed for {artist} - {track}: {e}")
            return []
