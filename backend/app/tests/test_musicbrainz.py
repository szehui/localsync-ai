"""Tests for the MusicBrainz client.

Covers the three behaviours Phase 0 proved are load-bearing: a mandatory
User-Agent, one limiter wait per request, and retry of the 503s that throttling
produces. Plus the query shaping that makes non-Latin titles findable.
"""
import httpx
import pytest

from app.services import musicbrainz as mb_module
from app.services.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzUnavailable,
    also_known_as,
    artist_credit_name,
    artist_mbid,
    first_release_year,
    is_requestable,
    normalize_query,
)
from app.services.rate_limit import AsyncRateLimiter

PSY_RG = {
    "id": "8ef6391e-0f1a-4c2b-9f8e-000000000001",
    "title": "싸다9",
    "primary-type": "Album",
    "secondary-types": [],
    "first-release-date": "2022-04-29",
    "artist-credit": [{"name": "PSY", "artist": {"id": "f1e1c1b1-0000-0000-0000-000000000001"}}],
    "releases": [
        {"title": "싸다9", "date": "2022-04-29", "track-count": 12},
        {"title": "PSY 9th", "date": "2022-04-29", "track-count": 12},
        {"title": "싸다9", "date": "2022-07-15", "track-count": 12},
    ],
}


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://mb.test/ws/2/x")
            raise httpx.HTTPStatusError(
                f"Server error '{self.status_code}'", request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self):
        return self._payload


class FakeClient:
    """Replays a scripted sequence of responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def get(self, url, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)

    async def aclose(self):
        return None


class CountingLimiter(AsyncRateLimiter):
    def __init__(self):
        super().__init__(0.0)
        self.waits = 0

    async def wait(self):
        self.waits += 1


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(mb_module.asyncio, "sleep", fake_sleep)
    return slept


def make_client(responses):
    limiter = CountingLimiter()
    fake = FakeClient(responses)
    return MusicBrainzClient(client=fake, limiter=limiter), fake, limiter


class TestUserAgentAndLimiter:
    async def test_user_agent_sent(self):
        client, fake, _ = make_client([FakeResponse(200, {"release-groups": []})])
        await client.search_release_groups("daft punk")
        assert fake.requests[0]["headers"]["User-Agent"].startswith("localsync-ai/")
        assert "github.com/szehui/localsync-ai" in fake.requests[0]["headers"]["User-Agent"]

    async def test_limiter_awaited_once_per_call(self):
        client, _, limiter = make_client([
            FakeResponse(200, {"release-groups": []}),
            FakeResponse(200, {"release-groups": []}),
        ])
        await client.search_release_groups("a")
        await client.search_release_groups("b")
        assert limiter.waits == 2


class TestRetry:
    async def test_retries_503_then_succeeds(self, no_sleep):
        client, fake, _ = make_client([
            FakeResponse(503),
            FakeResponse(200, {"release-groups": [PSY_RG]}),
        ])
        result = await client.search_release_groups("psy", requestable_only=False)
        assert len(result["release_groups"]) == 1
        assert len(fake.requests) == 2
        assert no_sleep  # backoff was applied

    async def test_raises_unavailable_after_exhausting_retries(self):
        client, fake, _ = make_client([FakeResponse(503) for _ in range(4)])
        with pytest.raises(MusicBrainzUnavailable):
            await client.search_release_groups("anything")

    async def test_404_returns_none_when_allowed(self):
        client, _, _ = make_client([FakeResponse(404)])
        assert await client.get_release_group("deadbeef") is None

    async def test_other_4xx_raises(self):
        client, _, _ = make_client([FakeResponse(400)])
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_release_group("bad")


class TestQueryShaping:
    def test_latin_passthrough(self):
        assert normalize_query("PSY 9th") == "PSY 9th"

    def test_hangul_fielded_and_stripped(self):
        assert normalize_query("화양연화 pt.1") == "releasegroup:(화양연화 pt 1)"

    def test_cjk_ideographs_fielded(self):
        assert normalize_query("八度空间") == "releasegroup:(八度空间)"

    def test_empty_stays_empty(self):
        assert normalize_query("   ") == ""

    async def test_search_uses_shaped_query(self):
        client, fake, _ = make_client([FakeResponse(200, {"release-groups": []})])
        await client.search_release_groups("화양연화 pt.1")
        assert fake.requests[0]["params"]["query"] == "releasegroup:(화양연화 pt 1)"

    async def test_empty_query_skips_the_call(self):
        client, fake, limiter = make_client([])
        result = await client.search_release_groups("   ")
        assert result == {"release_groups": [], "filtered_out": 0, "count": 0}
        assert fake.requests == []
        assert limiter.waits == 0


class TestHygiene:
    def test_album_is_requestable(self):
        assert is_requestable({"primary-type": "Album", "secondary-types": []})

    def test_ep_is_requestable(self):
        assert is_requestable({"primary-type": "EP", "secondary-types": []})

    def test_compilation_survives(self):
        assert is_requestable({"primary-type": "Album", "secondary-types": ["Compilation"]})

    def test_live_survives(self):
        assert is_requestable({"primary-type": "Album", "secondary-types": ["Live"]})

    def test_soundtrack_survives(self):
        assert is_requestable({"primary-type": "Album",
                               "secondary-types": ["Compilation", "Soundtrack"]})

    def test_interview_excluded(self):
        assert not is_requestable({"primary-type": "Album", "secondary-types": ["Interview"]})

    def test_audiobook_excluded(self):
        assert not is_requestable({"primary-type": "Audio drama",
                                   "secondary-types": ["Audiobook"]})

    def test_single_excluded_by_primary_type(self):
        assert not is_requestable({"primary-type": "Single", "secondary-types": []})

    async def test_filter_reports_how_many_hidden(self):
        client, _, _ = make_client([FakeResponse(200, {"release-groups": [
            PSY_RG,
            {"id": "x", "title": "An Interview", "primary-type": "Album",
             "secondary-types": ["Interview"]},
            {"id": "y", "title": "A Single", "primary-type": "Single",
             "secondary-types": []},
        ], "count": 3})])
        result = await client.search_release_groups("psy")
        assert len(result["release_groups"]) == 1
        assert result["filtered_out"] == 2

    async def test_requestable_only_false_keeps_everything(self):
        client, _, _ = make_client([FakeResponse(200, {"release-groups": [
            {"id": "y", "title": "A Single", "primary-type": "Single",
             "secondary-types": []},
        ], "count": 1})])
        result = await client.search_release_groups("x", requestable_only=False)
        assert len(result["release_groups"]) == 1
        assert result["filtered_out"] == 0


class TestBrowse:
    async def test_one_type_per_call(self):
        client, fake, _ = make_client([FakeResponse(200, {"release-groups": []})])
        await client.browse_artist_release_groups("artist-mbid", type_="album")
        params = fake.requests[0]["params"]
        assert params["type"] == "album"
        assert params["artist"] == "artist-mbid"


class TestDecorators:
    def test_also_known_as_finds_localised_title(self):
        assert also_known_as(PSY_RG) == ["PSY 9th"]

    def test_also_known_as_dedupes_and_excludes_canonical(self):
        detail = {"title": "Album", "releases": [
            {"title": "Album"},
            {"title": "Album (Deluxe)"},  # folds to "album deluxe", kept
            {"title": "Album"},
            {"title": "Other Name"},
        ]}
        assert also_known_as(detail) == ["Album (Deluxe)", "Other Name"]

    def test_also_known_as_empty(self):
        assert also_known_as({"title": "Album"}) == []

    def test_artist_credit_name(self):
        assert artist_credit_name(PSY_RG) == "PSY"

    def test_artist_mbid(self):
        assert artist_mbid(PSY_RG) == "f1e1c1b1-0000-0000-0000-000000000001"

    def test_artist_mbid_missing(self):
        assert artist_mbid({"artist-credit": []}) is None

    def test_first_release_year(self):
        assert first_release_year(PSY_RG) == 2022

    def test_first_release_year_missing(self):
        assert first_release_year({}) is None
