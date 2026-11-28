"""Tests for the Cover Art Archive client."""
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, CatalogCache
from app.services import catalog_cache
from app.services.coverart import front_cover_url, front_covers

MBID = "aa997ea0-1a1f-4e9e-a4e0-1b1b0f1f4d4f"

WITH_FRONT = {"images": [
    {"types": ["Back"], "front": False, "image": "http://caa/back.jpg"},
    {"types": ["Front"], "front": True,
     "image": "http://caa/front-original.jpg",
     "thumbnails": {"250": "http://caa/250.jpg", "500": "http://caa/500.jpg"}},
]}

NO_FRONT = {"images": [
    {"types": ["Booklet"], "front": False, "image": "http://caa/b.jpg"},
]}


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def get(self, url, **kwargs):
        self.requests.append(url)
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


async def test_returns_front_thumbnail_at_requested_size(db):
    client = FakeClient([FakeResponse(200, WITH_FRONT)])
    url = await front_cover_url(db, MBID, size="500", client=client)
    assert url == "http://caa/500.jpg"


async def test_falls_back_to_original_when_size_missing(db):
    client = FakeClient([FakeResponse(200, WITH_FRONT)])
    url = await front_cover_url(db, MBID, size="1200", client=client)
    assert url == "http://caa/front-original.jpg"


async def test_no_front_image_returns_none(db):
    client = FakeClient([FakeResponse(200, NO_FRONT)])
    assert await front_cover_url(db, MBID, client=client) is None


async def test_404_returns_none_and_is_cached(db):
    client = FakeClient([FakeResponse(404)])
    assert await front_cover_url(db, MBID, client=client) is None
    # Negative result cached: a second call must not touch the network.
    fresh = FakeClient([])
    assert await front_cover_url(db, MBID, client=fresh) is None
    assert fresh.requests == []
    assert db.query(CatalogCache).count() == 1


async def test_server_error_returns_none_but_is_not_cached(db):
    client = FakeClient([FakeResponse(503)])
    assert await front_cover_url(db, MBID, client=client) is None
    assert catalog_cache.get(db, catalog_cache.make_key("coverart", MBID, "500"),
                             "coverart") is None


async def test_network_error_returns_none(db):
    class BoomClient:
        async def get(self, url, **kwargs):
            raise httpx.ConnectError("boom")

        async def aclose(self):
            return None

    assert await front_cover_url(db, MBID, client=BoomClient()) is None


async def test_empty_mbid_short_circuits(db):
    client = FakeClient([])
    assert await front_cover_url(db, "", client=client) is None
    assert client.requests == []


async def test_positive_result_is_cached(db):
    client = FakeClient([FakeResponse(200, WITH_FRONT)])
    await front_cover_url(db, MBID, size="250", client=client)
    again = FakeClient([])
    assert await front_cover_url(db, MBID, size="250", client=again) == "http://caa/250.jpg"
    assert again.requests == []


async def test_front_covers_batch(db):
    client = FakeClient([FakeResponse(200, WITH_FRONT), FakeResponse(404)])
    result = await front_covers(db, [MBID, "other-mbid"], client=client)
    assert result[MBID] == "http://caa/500.jpg"
    assert result["other-mbid"] is None
