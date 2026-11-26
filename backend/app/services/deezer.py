"""Deezer album attribution.

YouTube Music's flat playlist feed carries no album metadata, so a missing
track cannot be handed to Lidarr without first answering "which release does
this song belong to?". Deezer's public search API answers that without a key
and without rate-limit pain.

Deezer is only ever used to *name* a release. Lidarr still resolves the actual
release group against MusicBrainz, so a bad attribution can only produce a
missing album, never a wrong download.
"""
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _norm(text: Optional[str]) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _similarity(a: str, b: str) -> float:
    """Token-overlap similarity in [0, 1] on already-normalized strings.

    Joiner words are ignored: Deezer credits "Florence + the Machine" where
    YouTube says "Florence and the Machine", and that difference alone must not
    sink an otherwise perfect match.
    """
    ignore = {"the", "and", "a", "an", "of"}
    ta = set(a.split()) - ignore
    tb = set(b.split()) - ignore
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


class DeezerService:
    """Track -> album attribution backed by Deezer's public search."""

    BASE_URL = "https://api.deezer.com"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._cache: Dict[tuple[str, str], Optional[Dict[str, Any]]] = {}

    async def lookup_album(
        self, artist: str, title: str, min_score: float = 0.8
    ) -> Optional[Dict[str, Any]]:
        """Return the release a track belongs to, or None if not confident.

        Result: ``{"album", "album_artist", "deezer_album_id", "score"}``.
        """
        key = (_norm(artist), _norm(title))
        if key in self._cache:
            return self._cache[key]

        rows = await self._search_tracks(artist, title)
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for row in rows:
            album = (row.get("album") or {}).get("title")
            row_artist = (row.get("artist") or {}).get("name", "")
            if not album:
                continue

            title_score = _similarity(_norm(title), _norm(row.get("title", "")))
            artist_score = _similarity(_norm(artist), _norm(row_artist))
            # A right title by the wrong artist is a different song.
            if artist and row_artist and artist_score < 0.5:
                continue

            score = title_score * 0.75 + artist_score * 0.25
            if score > best_score:
                best_score = score
                best = {
                    "album": album,
                    "album_artist": row_artist or artist,
                    "deezer_album_id": (row.get("album") or {}).get("id"),
                    "score": round(score, 3),
                }

        if best is not None and best_score < min_score:
            logger.info(
                "Deezer: no confident album for %s - %s (best %.2f: %s)",
                artist, title, best_score, best["album"],
            )
            best = None

        self._cache[key] = best
        return best

    async def _search_tracks(self, artist: str, title: str) -> List[Dict[str, Any]]:
        """Search Deezer tracks, most specific query first."""
        queries = []
        if artist and title:
            queries.append(f'artist:"{artist}" track:"{title}"')
        if artist and title:
            queries.append(f"{artist} {title}")
        elif title:
            queries.append(title)

        for query in queries:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(
                        f"{self.BASE_URL}/search", params={"q": query, "limit": 10}
                    )
                if resp.status_code != 200:
                    logger.warning(
                        "Deezer search HTTP %s for %r", resp.status_code, query
                    )
                    continue
                data = resp.json().get("data") or []
                if data:
                    return data
            except Exception as e:
                logger.warning("Deezer search failed for %r: %s", query, e)

        return []
