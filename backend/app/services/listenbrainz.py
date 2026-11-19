"""ListenBrainz API client.

Primary listening-history source for localsync-ai. Read-only: this client never
submits listens.

Two important quirks of the API, both learned live:

* **204 No Content returns an EMPTY body, not JSON.** The CF recommendations and
  statistics endpoints use 204 for "nothing computed yet". Parsing the body
  raises. Treat 204 as an empty list.
* **The Labs similar-recordings endpoint accepts exactly ONE recording MBID per
  call.** Comma-separated values are rejected with 400. Per-seed calls are
  therefore unavoidable, which is why callers must cache them.

Rate limits: main API allows 30 requests/window and asks for at most 1 call per
second. A User-Agent identifying the app is mandatory — requests without one may
be blocked without notice.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

LISTENBRAINZ_API_BASE = "https://api.listenbrainz.org"
LABS_API_BASE = "https://labs.api.listenbrainz.org"
USER_AGENT = "localsync-ai/0.2 ( https://github.com/szehui/localsync-ai )"

# Only this algorithm returned data in live testing; the other five advertised
# variants came back empty for the same seed, so default to the known-good one.
DEFAULT_LABS_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30"
)

MAX_ITEMS_PER_GET = 1000


class ListenBrainzError(Exception):
    """Base error for ListenBrainz API failures."""


class UserNotFoundError(ListenBrainzError):
    """The requested ListenBrainz user does not exist (HTTP 404)."""


class RateLimiter:
    """Spaces calls out and respects the server's advertised quota window.

    The API asks for at most one call per second. It also returns
    X-RateLimit-Remaining / X-RateLimit-Reset-In; when the quota is nearly
    exhausted we hold off until the window resets instead of eating a 429.
    """

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last_start = 0.0
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            target = max(self._last_start + self.min_interval, self._blocked_until)
            if target > now:
                await asyncio.sleep(target - now)
            self._last_start = time.monotonic()

    def observe(self, headers) -> None:
        """Extend the back-off window when the rate limit is nearly exhausted."""
        try:
            remaining = int(headers.get("X-RateLimit-Remaining", "-1"))
            reset_in = float(headers.get("X-RateLimit-Reset-In", "0"))
        except (TypeError, ValueError):
            return
        if remaining >= 0 and remaining <= 1 and reset_in > 0:
            self._blocked_until = max(self._blocked_until, time.monotonic() + reset_in)


class ListenBrainzClient:
    """Async read-only client for the ListenBrainz API."""

    def __init__(
        self,
        user_token: str,
        username: str,
        api_base: str = LISTENBRAINZ_API_BASE,
        labs_api_base: str = LABS_API_BASE,
        min_interval: float = 1.0,
        timeout: float = 20.0,
    ):
        self.user_token = user_token
        self.username = username
        self.api_base = api_base.rstrip("/")
        self.labs_api_base = labs_api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
        self._limiter = RateLimiter(min_interval)

    async def close(self) -> None:
        await self._client.aclose()

    # ── internals ────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Token {self.user_token}"}

    async def _get(self, url: str, params: dict | None = None,
                   authenticated: bool = True):
        """GET with rate limiting, 204 handling and typed 404 errors."""
        await self._limiter.acquire()
        headers = self._auth_headers() if authenticated else {}
        response = await self._client.get(url, params=params, headers=headers)
        self._limiter.observe(response.headers)

        if response.status_code == 404:
            raise UserNotFoundError(
                f"ListenBrainz user not found: {self.username}"
            )
        if response.status_code == 204:
            # Empty body — must not be parsed.
            return []
        response.raise_for_status()
        if not response.content or not response.content.strip():
            return []
        return response.json()

    @staticmethod
    def _normalize_listen(listen: dict) -> dict | None:
        """Flatten a raw listen into the shape the scheduler consumes.

        Prefers the server-resolved `mbid_mapping.recording_mbid` over the
        client-submitted `additional_info.recording_mbid`, because the former
        has been validated against MusicBrainz and the latter has not.
        """
        metadata = listen.get("track_metadata") or {}
        artist = (metadata.get("artist_name") or "").strip()
        title = (metadata.get("track_name") or "").strip()
        if not artist or not title:
            return None

        mapping = metadata.get("mbid_mapping") or {}
        additional = metadata.get("additional_info") or {}
        recording_mbid = (
            mapping.get("recording_mbid")
            or additional.get("recording_mbid")
            or ""
        ).strip() or None

        return {
            "artist": artist,
            "title": title,
            "album": metadata.get("release_name") or "",
            "listened_at": listen.get("listened_at"),
            "recording_mbid": recording_mbid,
            "release_mbid": mapping.get("release_mbid") or additional.get("release_mbid"),
            "artist_mbids": mapping.get("artist_mbids") or additional.get("artist_mbids") or [],
            "source_client": additional.get("submission_client"),
        }

    # ── core reads ───────────────────────────────────────────────────────────

    async def validate_token(self) -> dict:
        """Check the token. Returns {code, message, valid, user_name}."""
        return await self._get(f"{self.api_base}/1/validate-token") or {}

    async def get_listen_count(self) -> int:
        """Total number of listens on the account."""
        data = await self._get(f"{self.api_base}/1/user/{self.username}/listen-count")
        if not data:
            return 0
        return int((data.get("payload") or {}).get("count", 0))

    async def get_recent_listens(self, days: int = 7,
                                 count: int = MAX_ITEMS_PER_GET) -> list[dict]:
        """Fetch recent listens, newest first.

        One call: `count=1000` comfortably covers a week for normal listening.
        Listens that lack an artist or title are dropped, and unparseable ones
        degrade to recording_mbid=None rather than failing the whole fetch.
        """
        now = datetime.now(timezone.utc)
        min_ts = int((now - timedelta(days=days)).timestamp())
        data = await self._get(
            f"{self.api_base}/1/user/{self.username}/listens",
            params={"count": count, "min_ts": min_ts},
        )
        raw = (data or {}).get("payload", {}).get("listens", [])
        normalized = [self._normalize_listen(item) for item in raw]
        listens = [item for item in normalized if item is not None]

        logger.info(
            "ListenBrainz: fetched %d listens (%d usable) from the last %d days",
            len(raw), len(listens), days,
        )
        return listens
