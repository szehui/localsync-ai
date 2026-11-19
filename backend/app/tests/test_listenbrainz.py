"""Tests for the ListenBrainz client."""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.listenbrainz import (
    USER_AGENT,
    ListenBrainzClient,
    RateLimiter,
    UserNotFoundError,
)


def _response(status: int, body=None, content=None, headers=None) -> httpx.Response:
    kwargs = {"request": httpx.Request("GET", "http://test/")}
    if headers:
        kwargs["headers"] = headers
    if content is not None:
        kwargs["content"] = content
    elif body is not None:
        kwargs["json"] = body
    else:
        kwargs["content"] = b""
    return httpx.Response(status, **kwargs)


def _listens_payload(listens):
    return {"payload": {"count": len(listens), "listens": listens,
                        "user_id": "szeis4cookie"}}


@pytest.fixture
def client():
    # min_interval=0 so tests do not sleep
    return ListenBrainzClient(user_token="tok", username="szeis4cookie",
                              min_interval=0)


class TestRecentListens:
    @pytest.mark.asyncio
    async def test_prefers_mbid_mapping_over_submitted(self, client):
        """The server-resolved mbid_mapping wins over additional_info."""
        payload = _listens_payload([{
            "listened_at": 1771414109,
            "track_metadata": {
                "artist_name": "Röyksopp",
                "track_name": "Some Resolve",
                "release_name": "Senior",
                "additional_info": {"recording_mbid": "SUBMITTED",
                                    "submission_client": "navidrome"},
                "mbid_mapping": {"recording_mbid": "MAPPED",
                                 "release_mbid": "REL-MBID",
                                 "artist_mbids": ["A1"]},
            },
        }])
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, payload)
            listens = await client.get_recent_listens(days=7)

        assert len(listens) == 1
        entry = listens[0]
        assert entry["artist"] == "Röyksopp"
        assert entry["title"] == "Some Resolve"
        assert entry["album"] == "Senior"
        assert entry["recording_mbid"] == "MAPPED"
        assert entry["release_mbid"] == "REL-MBID"
        assert entry["artist_mbids"] == ["A1"]
        assert entry["source_client"] == "navidrome"

    @pytest.mark.asyncio
    async def test_falls_back_to_additional_info_mbid(self, client):
        """No mbid_mapping -> use the client-submitted recording_mbid."""
        payload = _listens_payload([{
            "listened_at": 1,
            "track_metadata": {
                "artist_name": "Artist",
                "track_name": "Title",
                "additional_info": {"recording_mbid": "SUBMITTED"},
            },
        }])
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, payload)
            listens = await client.get_recent_listens()

        assert listens[0]["recording_mbid"] == "SUBMITTED"

    @pytest.mark.asyncio
    async def test_no_mbid_at_all_is_none(self, client):
        """Unresolved listen -> recording_mbid None, but still returned."""
        payload = _listens_payload([{
            "listened_at": 1,
            "track_metadata": {"artist_name": "Artist", "track_name": "Title"},
        }])
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, payload)
            listens = await client.get_recent_listens()

        assert len(listens) == 1
        assert listens[0]["recording_mbid"] is None

    @pytest.mark.asyncio
    async def test_drops_listens_without_artist_or_title(self, client):
        payload = _listens_payload([
            {"listened_at": 1, "track_metadata": {"artist_name": "", "track_name": "Orphan"}},
            {"listened_at": 2, "track_metadata": {"artist_name": "Artist"}},
            {"listened_at": 3, "track_metadata": {"artist_name": "A", "track_name": "T"}},
        ])
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, payload)
            listens = await client.get_recent_listens()

        assert len(listens) == 1
        assert listens[0]["title"] == "T"

    @pytest.mark.asyncio
    async def test_requests_seven_day_window_and_max_count(self, client):
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, _listens_payload([]))
            before = datetime.now(timezone.utc)
            await client.get_recent_listens(days=7)
            after = datetime.now(timezone.utc)

        params = mock_get.call_args.kwargs["params"]
        assert params["count"] == 1000
        expected_min = int((before - timedelta(days=7)).timestamp())
        expected_max = int((after - timedelta(days=7)).timestamp())
        assert expected_min <= params["min_ts"] <= expected_max

    @pytest.mark.asyncio
    async def test_user_not_found_raises(self, client):
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(404, content=b"nope")
            with pytest.raises(UserNotFoundError):
                await client.get_recent_listens()


class TestNoContentHandling:
    """204 has an EMPTY body — parsing it raises. This bit the live probe."""

    @pytest.mark.asyncio
    async def test_204_returns_empty_list(self, client):
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(204, content=b"")
            assert await client.get_listen_count() == 0

    @pytest.mark.asyncio
    async def test_204_with_garbage_body_is_not_parsed(self, client):
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(204, content=b"this is not json")
            assert await client.get_recent_listens() == []


class TestHeaders:
    def test_user_agent_is_set(self, client):
        assert client._client.headers["user-agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_authorization_header_sent(self, client):
        with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = _response(200, {"code": 200, "valid": True})
            await client.validate_token()

        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Token tok"


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_enforces_minimum_interval(self):
        limiter = RateLimiter(min_interval=0.05)
        start = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        assert time.monotonic() - start >= 0.05

    def test_zero_interval_never_blocks(self):
        limiter = RateLimiter(min_interval=0)
        limiter.observe({"X-RateLimit-Remaining": "20", "X-RateLimit-Reset-In": "10"})
        assert limiter._blocked_until == 0.0

    def test_near_exhausted_quota_backs_off(self):
        limiter = RateLimiter(min_interval=0)
        limiter.observe({"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-In": "5"})
        assert limiter._blocked_until > time.monotonic()

    def test_missing_headers_are_ignored(self):
        limiter = RateLimiter(min_interval=0)
        limiter.observe({})
        assert limiter._blocked_until == 0.0
