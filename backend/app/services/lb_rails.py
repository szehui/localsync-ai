"""ListenBrainz rails: new releases and popularity.

ListenBrainz is the one upstream we already hold credentials and history for, so
these rails need no new account.

Two traps are encoded here rather than rediscovered later:

* The popularity endpoints are **POST-only** and take the ids in the body
  (``release_group_mbids`` / ``artist_mbids``). A GET returns 405 and a
  differently-named field returns 400.
* ``explore/fresh-releases`` is a large payload (~2 MB for a fortnight). It is
  cached for an hour and never fetched per page view.
"""
import logging
from typing import Iterable

import httpx

from app.services import catalog_cache
from app.services.musicbrainz import (
    artist_credit_name,
    artist_mbid,
    first_release_year,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.listenbrainz.org"
TIMEOUT = 30.0
CHUNK_SIZE = 25   # the API accepts up to 100; 25 keeps responses small
MAX_IDS = 100


def _rg_card(row: dict) -> dict:
    """Map a ListenBrainz fresh-release row onto the catalog card shape."""
    artist = row.get("artist_credit_name") or row.get("artist_name") or ""
    year = row.get("release_date")
    return {
        "release_group_mbid": row.get("release_group_mbid"),
        "title": row.get("release_group_name") or row.get("title") or "",
        "artist_name": artist,
        "artist_mbid": (row.get("artist_mbids") or [None])[0],
        "year": int(year[:4]) if isinstance(year, str) and year[:4].isdigit() else None,
        "primary_type": row.get("release_group_primary_type") or "Album",
        "secondary_types": row.get("release_group_secondary_type_list") or [],
        "cover_url": None,
        "score": None,
    }


async def fresh_releases(db, *, days: int = 14,
                         client: httpx.AsyncClient | None = None) -> list[dict]:
    """Recently released albums with a release-group MBID, deduped by MBID."""
    key = catalog_cache.make_key("rails-fresh", days)
    cached = catalog_cache.get(db, key, "rails")
    if cached is not None:
        return cached

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT)

    try:
        response = await client.get(f"{BASE_URL}/1/explore/fresh-releases/",
                                    params={"days": str(days)})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ListenBrainz fresh releases failed: %s", exc)
        return []
    finally:
        if owns_client:
            await client.aclose()

    seen: set[str] = set()
    cards: list[dict] = []
    for row in payload.get("payload", {}).get("releases") or []:
        card = _rg_card(row)
        mbid = card["release_group_mbid"]
        if not mbid or mbid in seen:
            continue
        seen.add(mbid)
        cards.append(card)

    catalog_cache.put(db, key, "rails", cards)
    return cards


def _chunks(ids: Iterable[str]) -> list[list[str]]:
    cleaned = [i for i in ids if i]
    return [cleaned[i:i + CHUNK_SIZE] for i in range(0, len(cleaned), CHUNK_SIZE)]


async def popularity(release_group_mbids: Iterable[str], *,
                     client: httpx.AsyncClient | None = None) -> dict[str, int]:
    """Map release-group MBID -> listen count.

    POST with ``{"release_group_mbids": [...]}``. Results are merged across
    chunks; unknown ids simply do not appear in the response.
    """
    chunks = _chunks(release_group_mbids)
    if not chunks:
        return {}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT)

    totals: dict[str, int] = {}
    try:
        for chunk in chunks:
            response = await client.post(
                f"{BASE_URL}/1/popularity/release-group",
                json={"release_group_mbids": chunk},
            )
            if response.status_code != 200:
                logger.warning("ListenBrainz popularity -> %s", response.status_code)
                continue
            for mbid, count in (response.json() or {}).items():
                totals[mbid] = int(count or 0)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ListenBrainz popularity failed: %s", exc)
    finally:
        if owns_client:
            await client.aclose()

    return totals


async def artist_popularity(artist_mbids: Iterable[str], *,
                            client: httpx.AsyncClient | None = None) -> dict[str, int]:
    """Same shape as :func:`popularity`, keyed by artist MBID."""
    chunks = _chunks(artist_mbids)
    if not chunks:
        return {}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT)

    totals: dict[str, int] = {}
    try:
        for chunk in chunks:
            response = await client.post(
                f"{BASE_URL}/1/popularity/artist",
                json={"artist_mbids": chunk},
            )
            if response.status_code != 200:
                logger.warning("ListenBrainz artist popularity -> %s", response.status_code)
                continue
            for mbid, count in (response.json() or {}).items():
                totals[mbid] = int(count or 0)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ListenBrainz artist popularity failed: %s", exc)
    finally:
        if owns_client:
            await client.aclose()

    return totals
