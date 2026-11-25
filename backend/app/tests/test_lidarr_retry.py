"""Tests for Lidarr request retries.

Lidarr proxies MusicBrainz through its own rate-limited metadata proxy and
answers **503 Service Unavailable** when that proxy is saturated. Verified live:
`album/lookup?term=Ellie Goulding Halcyon` returned 503, and the identical
request 6s later returned 10 results. Clicking "Add album" a few times in a row
is enough to trigger it, so the client must retry rather than surface it.
"""
import httpx
import pytest

from app.services import lidarr as lidarr_module
from app.services.lidarr import LidarrService


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}" if payload is None else b'{"x":1}'

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://lidarr.test/api/v1/x")
            raise httpx.HTTPStatusError(
                f"Server error '{self.status_code}'", request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self):
        return self._payload


class FakeClient:
    """Replays a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry backoff must not actually sleep in tests."""
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(lidarr_module.asyncio, "sleep", fake_sleep)
    return slept


def patch_client(monkeypatch, client):
    monkeypatch.setattr(
        lidarr_module.httpx, "AsyncClient", lambda *a, **k: client
    )
    return client


class TestRetryOnThrottle:
    async def test_retries_503_then_succeeds(self, monkeypatch, no_sleep):
        client = FakeClient([FakeResponse(503), FakeResponse(200, [{"ok": True}])])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        result = await service.lookup_album("Ellie Goulding Halcyon")

        assert result == [{"ok": True}]
        assert len(client.requests) == 2
        assert len(no_sleep) == 1  # exactly one backoff observed

    async def test_retries_429_and_502_too(self, monkeypatch):
        client = FakeClient([
            FakeResponse(429), FakeResponse(502), FakeResponse(200, [{"ok": True}]),
        ])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        assert await service.lookup_album("x") == [{"ok": True}]
        assert len(client.requests) == 3

    async def test_gives_up_after_backoff_exhausted(self, monkeypatch):
        attempts = len(lidarr_module.RETRY_BACKOFF) + 1
        client = FakeClient([FakeResponse(503) for _ in range(attempts)])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await service.lookup_album("x")

        assert exc.value.response.status_code == 503
        assert len(client.requests) == attempts

    async def test_does_not_retry_client_errors(self, monkeypatch):
        """A 400 means the request itself is wrong — retrying cannot help."""
        client = FakeClient([FakeResponse(400)])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        with pytest.raises(httpx.HTTPStatusError):
            await service.lookup_album("x")
        assert len(client.requests) == 1

    async def test_does_not_retry_404(self, monkeypatch):
        client = FakeClient([FakeResponse(404)])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        with pytest.raises(httpx.HTTPStatusError):
            await service.lookup_album("x")
        assert len(client.requests) == 1

    async def test_backoff_is_cumulative_and_bounded(self, monkeypatch, no_sleep):
        attempts = len(lidarr_module.RETRY_BACKOFF) + 1
        client = FakeClient([FakeResponse(503) for _ in range(attempts)])
        patch_client(monkeypatch, client)

        service = LidarrService("http://lidarr.test", "key")
        with pytest.raises(httpx.HTTPStatusError):
            await service.lookup_album("x")

        # Jitter is added on top, so each delay is >= its base.
        for observed, base in zip(no_sleep, lidarr_module.RETRY_BACKOFF):
            assert base <= observed <= base + 0.5
        assert sum(lidarr_module.RETRY_BACKOFF) <= 12
