"""Navidrome native REST client, for the one field Subsonic cannot give us.

The Subsonic API (``/rest/…``) exposes ``musicBrainzId``, which is the **release**
MBID. Lidarr keys albums on the **release group** MBID, and in this library every
album that carries both carries *different* values — so matching those two would
have been silently always wrong.

Navidrome's own API (``/api/…``) returns ``mbzReleaseGroupId`` and ``mbzAlbumId``
on every album row, in one paged walk. That is the only source of the tag tier.

Auth is a JSON login returning a bearer token, not Subsonic's salted MD5 params.
"""
import logging
import time
from datetime import datetime
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 12 * 3600
PAGE_SIZE = 200


class NavidromeNativeAuthError(RuntimeError):
    """Raised when the native API rejects the stored credentials."""


class NavidromeNative:
    """Minimal async client for Navidrome's own (non-Subsonic) API."""

    def __init__(self, url: str, username: str, password: str, *,
                 client: Optional[httpx.AsyncClient] = None, timeout: float = 30.0):
        self.base_url = (url or "").rstrip("/")
        self.username = username
        self.password = password
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        self.reauths = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _login(self) -> str:
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.base_url}/auth/login",
                json={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise NavidromeNativeAuthError(f"Navidrome unreachable: {exc}") from exc
        if response.status_code != 200:
            raise NavidromeNativeAuthError(
                f"Navidrome login failed ({response.status_code})"
            )
        token = (response.json() or {}).get("token")
        if not token:
            raise NavidromeNativeAuthError("Navidrome login returned no token")
        self._token = token
        self._token_at = time.monotonic()
        return token

    async def _auth_token(self) -> str:
        if self._token and (time.monotonic() - self._token_at) < TOKEN_TTL_SECONDS:
            return self._token
        return await self._login()

    async def _get(self, path: str, params: dict | None = None) -> object:
        client = await self._get_client()
        token = await self._auth_token()
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        response = await client.get(
            url, params=params or {},
            headers={"x-nd-authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            # Token expired or was revoked: log in again exactly once. A second
            # 401 means the credentials themselves are bad, and retrying forever
            # would hammer a server that is telling us no.
            self.reauths += 1
            if self.reauths > 1:
                raise NavidromeNativeAuthError("Navidrome rejected renewed token")
            token = await self._login()
            response = await client.get(
                url, params=params or {},
                headers={"x-nd-authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                raise NavidromeNativeAuthError("Navidrome rejected renewed token")
        response.raise_for_status()
        return response.json()

    async def iter_albums(self, page_size: int = PAGE_SIZE) -> AsyncIterator[dict]:
        """Yield every album row, one page at a time."""
        start = 0
        while True:
            page = await self._get("album", {
                "_start": start, "_end": start + page_size, "_sort": "name",
            })
            if not isinstance(page, list) or not page:
                return
            for row in page:
                yield row
            if len(page) < page_size:
                return
            start += page_size

    async def list_albums(self, page_size: int = PAGE_SIZE) -> list[dict]:
        return [row async for row in self.iter_albums(page_size)]

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


async def sync_album_mbids(db, native: NavidromeNative) -> dict:
    """Copy release-group MBIDs from Navidrome onto our album rows.

    Cheap and authoritative — these are the tags themselves, so the result is
    marked ``mbid_source="navidrome_tag"`` at ``high`` confidence.

    Never raises on a Navidrome failure: a library sync that succeeded must not
    be reported as failed because an optional enrichment pass could not run.
    """
    from app.models.database import Album

    stats = {"albums": 0, "tagged": 0, "updated": 0, "missing_rows": 0}
    try:
        existing = {row.id: row for row in db.query(Album).all()}
        async for row in native.iter_albums():
            stats["albums"] += 1
            album = existing.get(row.get("id"))
            if album is None:
                stats["missing_rows"] += 1
                continue

            rg = row.get("mbzReleaseGroupId")
            release = row.get("mbzAlbumId")
            if not rg:
                continue
            stats["tagged"] += 1

            changed = False
            if album.release_group_mbid != rg:
                album.release_group_mbid = rg
                # Only a tag may overwrite another tag or a weaker guess; a
                # previously validated oracle id is not downgraded.
                if album.mbid_source in (None, "navidrome_tag", "musicbrainz_backfill"):
                    album.mbid_source = "navidrome_tag"
                    album.mbid_confidence = "high"
                changed = True
            if release and album.mbz_album_id != release:
                album.mbz_album_id = release
                changed = True
            # Navidrome's album object has no `year` field at all; `maxYear` is
            # the only usable date. The library sync reads Subsonic's `year`,
            # which is None for every album, so fill it here. Used only as a
            # tiebreaker between same-title candidates -- never to reject a match.
            max_year = row.get("maxYear")
            if max_year and not album.year:
                album.year = int(max_year)
                changed = True
            if changed:
                album.mbid_checked_at = datetime.utcnow()
                stats["updated"] += 1
        db.commit()
    except Exception as exc:  # noqa: BLE001 - enrichment must never fail a sync
        db.rollback()
        logger.warning("Album MBID tag pass failed: %s", exc)
        stats["error"] = str(exc)
        return stats

    logger.info(
        "Album MBID tag pass: %d albums, %d tagged, %d updated",
        stats["albums"], stats["tagged"], stats["updated"],
    )
    return stats
