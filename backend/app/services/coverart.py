"""Cover Art Archive client.

Artwork is what makes a catalog card recognizable — especially for albums whose
title the user does not read in that script.

The API is free and needs no key, but it 404s often: a release group with no
cover is normal, not an error. Negatives are cached too, because otherwise every
render of the same card re-asks for artwork that will never exist.
"""
import logging
from typing import Optional

import httpx

from app.services import catalog_cache

logger = logging.getLogger(__name__)

BASE_URL = "https://coverartarchive.org"
TIMEOUT = 15.0


def _pick_url(payload: dict, size: str) -> Optional[str]:
    """Front image at the requested thumbnail size, falling back to the original."""
    for image in payload.get("images") or []:
        if not image.get("front"):
            continue
        thumbnails = image.get("thumbnails") or {}
        return thumbnails.get(size) or image.get("image")
    return None


async def front_cover_url(db, release_group_mbid: str, *, size: str = "500",
                          client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """Return a front-cover URL for a release group, or None.

    Never raises on a missing cover: a 404 is an answer, not a failure, and the
    UI renders a placeholder for it.
    """
    if not release_group_mbid:
        return None

    key = catalog_cache.make_key("coverart", release_group_mbid, size)
    cached = catalog_cache.get(db, key, "coverart")
    if cached is not None:
        return cached.get("url")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)

    url = None
    try:
        response = await client.get(f"{BASE_URL}/release-group/{release_group_mbid}")
        if response.status_code == 200:
            url = _pick_url(response.json(), size)
        elif response.status_code != 404:
            # 5xx and friends: do not cache, so a later attempt can succeed.
            logger.warning("Cover Art Archive %s -> %s",
                           release_group_mbid, response.status_code)
            return None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Cover Art Archive lookup failed for %s: %s",
                       release_group_mbid, exc)
        return None
    finally:
        if owns_client:
            await client.aclose()

    catalog_cache.put(db, key, "coverart", {"url": url})
    return url


async def front_covers(db, release_group_mbids: list[str], *, size: str = "500",
                       client: Optional[httpx.AsyncClient] = None) -> dict[str, Optional[str]]:
    """Batch helper. Sequential on purpose — CAA has no batch endpoint and a
    search page only ever needs the first handful of cards."""
    out: dict[str, Optional[str]] = {}
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        for mbid in release_group_mbids:
            out[mbid] = await front_cover_url(db, mbid, size=size, client=client)
    finally:
        if owns_client:
            await client.aclose()
    return out
