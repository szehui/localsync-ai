"""MusicBrainz web service client.

MusicBrainz is the only catalog that carries the identifier Lidarr accepts, so it
is the spine of the request system: a user picks a release group, we store its
MBID, and Lidarr resolves that exact id.

Two behaviours are not optional, both learned the hard way:

* **Serialise and retry.** MB allows ~1 req/s and answers 503 to bursts. Phase 0
  lost a probe run to this — 35 retries across 101 calls, with several "no match"
  rows that were really just throttling. Every request goes through the shared
  ``AsyncRateLimiter`` and retries ``{429, 502, 503, 504}``.
* **Shape the query for the script.** ``releasegroup:"화양연화 pt.1"`` returns 503
  because punctuation inside a quoted field trips the analyzer; the field-scoped,
  punctuation-stripped ``releasegroup:(화양연화)`` returns all three release groups.
  Latin queries are passed through untouched.

Failure is surfaced as ``MusicBrainzUnavailable`` so routers can answer 503 with a
readable message instead of a 500.
"""
import asyncio
import logging
import random
from typing import Any, Optional

import httpx

from app.services.rate_limit import AsyncRateLimiter
from app.services.title_norm import fold, has_cjk, strip_punctuation

logger = logging.getLogger(__name__)

BASE_URL = "https://musicbrainz.org/ws/2"
VERSION = "0.1.0"
USER_AGENT = f"localsync-ai/{VERSION} ( https://github.com/szehui/localsync-ai )"

RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_BACKOFF = (1.0, 3.0, 6.0)

# Audiobooks, interviews and DJ sets are release groups too, but nobody wants
# them queued for download.
JUNK_SECONDARY = frozenset({
    "interview", "audiobook", "spokenword", "audio drama", "demo",
    "dj-mix", "mixtape/street", "field recording",
})
REQUESTABLE_PRIMARY = frozenset({"album", "ep"})


class MusicBrainzUnavailable(RuntimeError):
    """Raised when MusicBrainz stays unreachable after retries."""


def is_requestable(rg: dict) -> bool:
    """True when this release group is something a person would request.

    Compilations and live albums survive: they are legitimate things to own. An
    interview or an audiobook does not. Singles are excluded because the primary
    type is not album/EP — callers that want them pass ``requestable_only=False``.
    """
    primary = (rg.get("primary-type") or "").lower()
    if primary not in REQUESTABLE_PRIMARY:
        return False
    secondary = {s.lower() for s in (rg.get("secondary-types") or [])}
    return not (secondary & JUNK_SECONDARY)


def normalize_query(text: str) -> str:
    """Shape a user query for MB's Lucene parser.

    Non-Latin input is field-scoped with punctuation stripped — that is the only
    form that survives the analyzer. Everything else is passed through so MB's
    own ``score`` ordering stays meaningful (``PSY 9th`` puts 싸다9 at rank 2).
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    if has_cjk(text):
        stripped = strip_punctuation(text)
        return f"releasegroup:({stripped})" if stripped else text
    return text


def also_known_as(detail: dict) -> list[str]:
    """Alternate titles from a release group's releases.

    A release group's releases carry localised titles: ``싸다9`` contains three
    official releases, one of them titled ``PSY 9th``. Collecting those titles is
    what lets a household member find a Korean album by its English name — and it
    is MusicBrainz-native, so it needs no Deezer round trip and no fuzzy match.

    Titles identical to the group title after folding are dropped.
    """
    canonical = fold(detail.get("title") or "")
    seen: set[str] = set()
    out: list[str] = []
    for release in detail.get("releases") or []:
        title = (release.get("title") or "").strip()
        if not title:
            continue
        key = fold(title)
        if not key or key == canonical or key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def artist_credit_name(rg: dict) -> str:
    return " ".join(c.get("name") or "" for c in rg.get("artist-credit") or [])


def artist_mbid(rg: dict) -> Optional[str]:
    for credit in rg.get("artist-credit") or []:
        artist = credit.get("artist") or {}
        if artist.get("id"):
            return artist["id"]
    return None


def first_release_year(rg: dict) -> Optional[int]:
    date = (rg.get("first-release-date") or "").strip()
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


class MusicBrainzClient:
    """Async MusicBrainz client: one shared limiter, retries, mandatory UA."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        user_agent: str = USER_AGENT,
        limiter: Optional[AsyncRateLimiter] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._limiter = limiter or AsyncRateLimiter(1.0)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self.call_count = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _request(self, path: str, params: dict[str, Any] | None = None,
                       *, allow_404: bool = False) -> Optional[dict]:
        client = await self._get_client()
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {**(params or {}), "fmt": "json"}
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}

        attempts = len(RETRY_BACKOFF) + 1
        for attempt in range(attempts):
            await self._limiter.wait()
            self.call_count += 1
            try:
                response = await client.get(url, params=query, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < attempts - 1:
                    delay = RETRY_BACKOFF[attempt] + random.uniform(0, 0.5)
                    logger.warning("MusicBrainz %s network error (%s), retrying in %.1fs",
                                   path, exc, delay)
                    await asyncio.sleep(delay)
                    continue
                raise MusicBrainzUnavailable(f"MusicBrainz unreachable: {exc}") from exc

            if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
                delay = RETRY_BACKOFF[attempt] + random.uniform(0, 0.5)
                logger.warning("MusicBrainz %s -> %s, retrying in %.1fs (attempt %d/%d)",
                               path, response.status_code, delay, attempt + 1, attempts)
                await asyncio.sleep(delay)
                continue

            if response.status_code == 404 and allow_404:
                return None
            if response.status_code in RETRY_STATUSES:
                raise MusicBrainzUnavailable(
                    f"MusicBrainz returned {response.status_code} for {path}"
                )
            response.raise_for_status()
            return response.json()

        raise MusicBrainzUnavailable(f"MusicBrainz gave up on {path}")  # pragma: no cover

    # ── reads ───────────────────────────────────────────────────────────────

    async def search_release_groups(self, query: str, *, limit: int = 10,
                                    offset: int = 0,
                                    requestable_only: bool = True) -> dict:
        """Search release groups and report how many results were filtered out.

        Filtering happens *after* MB scoring, so the surviving order is MB's own
        relevance order — re-ranking here would undo the one signal that works
        for non-Latin titles.
        """
        shaped = normalize_query(query)
        if not shaped:
            return {"release_groups": [], "filtered_out": 0, "count": 0}

        data = await self._request("release-group/", {
            "query": shaped,
            "limit": str(limit),
            "offset": str(offset),
        }) or {}

        raw = data.get("release-groups") or []
        if not requestable_only:
            return {"release_groups": raw, "filtered_out": 0,
                    "count": data.get("count", len(raw))}

        kept = [rg for rg in raw if is_requestable(rg)]
        return {
            "release_groups": kept,
            "filtered_out": len(raw) - len(kept),
            "count": data.get("count", len(raw)),
        }

    async def get_release_group(self, mbid: str, *,
                                inc: str = "artist-credits+releases") -> Optional[dict]:
        return await self._request(f"release-group/{mbid}", {"inc": inc},
                                   allow_404=True)

    async def get_artist(self, mbid: str, *, inc: str = "url-rels") -> Optional[dict]:
        return await self._request(f"artist/{mbid}", {"inc": inc}, allow_404=True)

    async def search_artists(self, query: str, *, limit: int = 5) -> list[dict]:
        shaped = normalize_query(query)
        if not shaped:
            return []
        data = await self._request("artist/", {"query": shaped, "limit": str(limit)}) or {}
        return data.get("artists") or []

    async def browse_artist_release_groups(self, artist_mbid_: str, *,
                                           type_: str = "album", limit: int = 100,
                                           offset: int = 0) -> list[dict]:
        """Browse one artist's release groups of a single primary type.

        One ``type`` per call: repeating the parameter returns 503 (Phase 0).
        """
        data = await self._request("release-group", {
            "artist": artist_mbid_,
            "type": type_,
            "limit": str(limit),
            "offset": str(offset),
            "inc": "artist-credits",
        }) or {}
        return data.get("release-groups") or []

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
