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


# Row keys copied from a live /1/explore/fresh-releases/ response on 2026-11-27.
# Note the absence of "release_group_name"/"title": the real feed names albums
# in "release_name". The previous fixture invented those keys, which is how a
# blank-title bug survived a green test suite.
#   artist_credit_name, artist_mbids, caa_id, caa_release_mbid, listen_count,
#   release_date, release_group_mbid, release_group_primary_type, release_mbid,
#   release_name, release_tags
FRESH_PAYLOAD = {"payload": {"releases": [
    {"release_group_mbid": "rg-1", "release_name": "Album One",
     "artist_credit_name": "Artist One", "artist_mbids": ["artist-1"],
     "release_date": "2026-11-20", "release_group_primary_type": "Album",
     "caa_release_mbid": "rel-1", "listen_count": 12},
    {"release_group_mbid": "rg-1", "release_name": "Album One dup",
     "artist_credit_name": "Artist One", "release_date": "2026-11-20"},
    {"release_group_mbid": None, "release_name": "No MBID"},
    {"release_group_mbid": "rg-2", "release_name": "Album Two",
     "artist_credit_name": "Artist Two", "artist_mbids": ["artist-2"],
     "release_date": "2026-11-21", "caa_release_mbid": None,
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

    async def test_title_falls_back_to_release_name(self, db):
        # The live feed has no release_group_name at all; a blank title in the
        # rails is a user-visible bug, so assert the fallback directly.
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        for card in await lb_rails.fresh_releases(db, client=client):
            assert card["title"], "every rail card must have a non-empty title"

    async def test_cover_url_built_from_caa_release_mbid(self, db):
        client = FakeClient([FakeResponse(200, FRESH_PAYLOAD)])
        cards = await lb_rails.fresh_releases(db, client=client)
        assert cards[0]["cover_url"] == (
            "https://coverartarchive.org/release/rel-1/front-250")
        assert cards[1]["cover_url"] is None

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


def _live_rg(mbid: str, count: int) -> dict:
    """One entry in the real payload — copied from a live call on 2026-11-27.

    The endpoint returns a LIST of objects, not a dict keyed by MBID. The old
    tests asserted the dict shape, so they passed while the rails endpoint was
    500ing against production.
    """
    return {"release_group_mbid": mbid, "total_listen_count": count,
            "total_user_count": max(1, count // 100)}


def _live_artist(mbid: str, count: int) -> dict:
    return {"artist_mbid": mbid, "total_listen_count": count,
            "total_user_count": max(1, count // 100)}


class TestPopularity:
    async def test_parses_live_list_shape(self):
        client = FakeClient([FakeResponse(200, [_live_rg("rg-1", 42)])])
        result = await lb_rails.popularity(["rg-1"], client=client)
        assert result == {"rg-1": 42}
        assert client.posts[0]["url"].endswith("/1/popularity/release-group")
        assert client.posts[0]["json"] == {"release_group_mbids": ["rg-1"]}

    async def test_still_accepts_dict_payload(self):
        # Tolerated for older/newer API shapes; must not raise.
        client = FakeClient([FakeResponse(200, {"rg-1": 42})])
        assert await lb_rails.popularity(["rg-1"], client=client) == {"rg-1": 42}

    async def test_unexpected_payload_is_empty_not_fatal(self):
        client = FakeClient([FakeResponse(200, "nonsense")])
        assert await lb_rails.popularity(["rg-1"], client=client) == {}

    async def test_entries_without_count_default_to_zero(self):
        client = FakeClient([FakeResponse(200, [{"release_group_mbid": "rg-1"}])])
        assert await lb_rails.popularity(["rg-1"], client=client) == {"rg-1": 0}

    async def test_chunks_at_twenty_five(self):
        ids = [f"rg-{i}" for i in range(60)]
        client = FakeClient([FakeResponse(200, []), FakeResponse(200, []),
                             FakeResponse(200, [])])
        await lb_rails.popularity(ids, client=client)
        sizes = [len(p["json"]["release_group_mbids"]) for p in client.posts]
        assert sizes == [25, 25, 10]

    async def test_empty_list_makes_no_call(self):
        client = FakeClient([])
        assert await lb_rails.popularity([], client=client) == {}
        assert client.posts == []

    async def test_filters_empty_ids(self):
        client = FakeClient([FakeResponse(200, [])])
        await lb_rails.popularity(["rg-1", "", None], client=client)
        assert client.posts[0]["json"]["release_group_mbids"] == ["rg-1"]

    async def test_partial_chunk_failure_keeps_others(self):
        client = FakeClient([FakeResponse(200, [_live_rg("a", 1)]), FakeResponse(503),
                             FakeResponse(200, [_live_rg("c", 3)])])
        ids = [f"rg-{i}" for i in range(60)]
        result = await lb_rails.popularity(ids, client=client)
        assert result == {"a": 1, "c": 3}

    async def test_artist_popularity_body_field(self):
        client = FakeClient([FakeResponse(200, [_live_artist("artist-1", 7)])])
        result = await lb_rails.artist_popularity(["artist-1"], client=client)
        assert result == {"artist-1": 7}
        assert client.posts[0]["json"] == {"artist_mbids": ["artist-1"]}
        assert client.posts[0]["url"].endswith("/1/popularity/artist")
