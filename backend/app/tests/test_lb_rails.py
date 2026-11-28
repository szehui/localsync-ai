"""Tests for the ListenBrainz rails.

The POST body shape is asserted deliberately: the popularity endpoints answer
405 to GET and 400 to a differently-named body field, so a silent regression
here would look like "no popularity data" rather than a bug.
"""
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.services import lb_rails


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.listenbrainz.org/1/x")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.gets = []
        self.posts = []

    async def get(self, url, params=None):
        self.gets.append({"url": url, "params": params})
        return self.responses.pop(0)

    async def post(self, url, json=None):
        self.posts.append({"url": url, "json": json})
        return self.responses.pop(0)

    async def aclose(self):
        return None


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


FRESH_PAYLOAD = {"payload": {"releases": [
    {"release_group_mbid": "rg-1", "release_group_name": "Album One",
     "artist_credit_name": "Artist One", "artist_mbids": ["artist-1"],
     "release_date": "2026-11-20", "release_group_primary_type": "Album"},
    {"release_group_mbid": "rg-1", "release_group_name": "Album One dup",
     "artist_credit_name": "Artist One", "release_date": "2026-11-20"},
    {"release_group_mbid": None, "release_group_name": "No MBID"},
    {"release_group_mbid": "rg-2", "release_group_name": "Album Two",
     "artist_credit_name": "Artist Two", "artist_mbids": ["artist-2"],
     "release_date": "2026-11-21",
     "release_group_secondary_type_list": ["Live"]},
]}}


class TestFreshReleases:
    async def test_maps_and_dedupes(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        cards = await lb_rails.fresh_releases(db, client=client)
        assert [c["release_group_mbid"] for c in cards] == ["rg-1", "rg-2"]

    async def test_drops_rows_without_mbid(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        cards = await lb_rails.fresh_releases(db, client=client)
        assert all(c["release_group_mbid"] for c in cards)

    async def test_card_shape(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        card = (await lb_rails.fresh_releases(db, client=client))[0]
        assert card["title"] == "Album One"
        assert card["artist_name"] == "Artist One"
        assert card["artist_mbid"] == "artist-1"
        assert card["year"] == 2026
        assert card["secondary_types"] == []

    async def test_secondary_types_preserved(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        cards = await lb_rails.fresh_releases(db, client=client)
        assert cards[1]["secondary_types"] == ["Live"]

    async def test_cached_for_an_hour(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        await lb_rails.fresh_releases(db, client=client)
        again = FakeClient([])
        cards = await lb_rails.fresh_releases(db, client=again)
        assert len(cards) == 2
        assert again.gets == []

    async def test_http_error_returns_empty(self, db):
        client = FakeClient([FakeResponse(500)])
        assert await lb_rails.fresh_releases(db, client=client) == []


class TestPopularity:
    async def test_posts_release_group_mbids(self):
        client = FakeClient([FakeResponse(200, {"rg-1": 42})])
        result = await lb_rails.popularity(["rg-1"], client=client)
        assert result == {"rg-1": 42}
        assert client.posts[0]["url"].endswith("/1/popularity/release-group")
        assert client.posts[0]["json"] == {"release_group_mbids": ["rg-1"]}

    async def test_chunks_at_twenty_five(self):
        ids = [f"rg-{i}" for i in range(60)]
        client = FakeClient([FakeResponse(200, {}), FakeResponse(200, {}),
                             FakeResponse(200, {})])
        await lb_rails.popularity(ids, client=client)
        sizes = [len(p["json"]["release_group_mbids"]) for p in client.posts]
        assert sizes == [25, 25, 10]

    async def test_empty_list_makes_no_call(self):
        client = FakeClient([])
        assert await lb_rails.popularity([], client=client) == {}
        assert client.posts == []

    async def test_filters_empty_ids(self):
        client = FakeClient([FakeResponse(200, {})])
        await lb_rails.popularity(["rg-1", "", None], client=client)
        assert client.posts[0]["json"]["release_group_mbids"] == ["rg-1"]

    async def test_partial_chunk_failure_keeps_others(self):
        client = FakeClient([FakeResponse(200, {"a": 1}), FakeResponse(503),
                             FakeResponse(200, {"c": 3})])
        ids = [f"rg-{i}" for i in range(60)]
        result = await lb_rails.popularity(ids, client=client)
        assert result == {"a": 1, "c": 3}

    async def test_artist_popularity_body_field(self):
        client = FakeClient([FakeResponse(200, {"artist-1": 7})])
        result = await lb_rails.artist_popularity(["artist-1"], client=client)
        assert result == {"artist-1": 7}
        assert client.posts[0]["json"] == {"artist_mbids": ["artist-1"]}
        assert client.posts[0]["url"].endswith("/1/popularity/artist")
