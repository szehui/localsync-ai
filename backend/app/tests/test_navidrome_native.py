"""Tests for the Navidrome native client and the tag-tier MBID pass."""
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Album, Base
from app.services.navidrome_native import (
    NavidromeNative,
    NavidromeNativeAuthError,
    sync_album_mbids,
)

RG = "aa997ea0-1a1f-4e9e-a4e0-1b1b0f1f4d4f"
REL = "11111111-2222-3333-4444-555555555555"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://nd/api/album")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class FakeClient:
    def __init__(self, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []

    async def post(self, url, json=None):
        self.post_calls.append({"url": url, "json": json})
        return self.posts.pop(0)

    async def get(self, url, params=None, headers=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return self.gets.pop(0)

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


def album_row(**kwargs):
    base = {"id": "al1", "name": "Album One", "albumArtist": "Artist One",
            "mbzReleaseGroupId": RG, "mbzAlbumId": REL}
    base.update(kwargs)
    return base


class TestAuth:
    async def test_login_then_page(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "tok-1"})],
            gets=[FakeResponse(200, [album_row()])],
        )
        native = NavidromeNative("http://nd", "sze", "pw", client=client)
        albums = await native.list_albums()
        assert len(albums) == 1
        assert client.post_calls[0]["json"] == {"username": "sze", "password": "pw"}
        assert client.get_calls[0]["headers"]["x-nd-authorization"] == "Bearer tok-1"

    async def test_token_is_reused_across_pages(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "tok-1"})],
            gets=[FakeResponse(200, [album_row(id=f"al{i}") for i in range(2)]),
                  FakeResponse(200, [album_row(id="al2")])],
        )
        native = NavidromeNative("http://nd", "sze", "pw", client=client)
        albums = await native.list_albums(page_size=2)
        assert len(albums) == 3
        assert len(client.post_calls) == 1

    async def test_bad_credentials_raise(self):
        client = FakeClient(posts=[FakeResponse(401, {"error": "nope"})])
        native = NavidromeNative("http://nd", "sze", "wrong", client=client)
        with pytest.raises(NavidromeNativeAuthError):
            await native.list_albums()

    async def test_401_triggers_one_relogin(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "old"}),
                   FakeResponse(200, {"token": "new"})],
            gets=[FakeResponse(401, {"error": "expired"}),
                  FakeResponse(200, [album_row()])],
        )
        native = NavidromeNative("http://nd", "sze", "pw", client=client)
        albums = await native.list_albums()
        assert len(albums) == 1
        assert len(client.post_calls) == 2
        assert client.get_calls[1]["headers"]["x-nd-authorization"] == "Bearer new"

    async def test_401_twice_gives_up(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "a"}), FakeResponse(200, {"token": "b"})],
            gets=[FakeResponse(401), FakeResponse(401)],
        )
        native = NavidromeNative("http://nd", "sze", "pw", client=client)
        with pytest.raises(NavidromeNativeAuthError):
            await native.list_albums()


class TestPaging:
    async def test_short_page_stops(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [album_row()])],
        )
        native = NavidromeNative("http://nd", "u", "p", client=client)
        assert len(await native.list_albums(page_size=200)) == 1
        assert len(client.get_calls) == 1

    async def test_full_page_requests_next(self):
        rows = [album_row(id=f"al{i}") for i in range(2)]
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, rows), FakeResponse(200, [])],
        )
        native = NavidromeNative("http://nd", "u", "p", client=client)
        await native.list_albums(page_size=2)
        assert client.get_calls[1]["params"]["_start"] == 2

    async def test_empty_response_stops(self):
        client = FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [])],
        )
        native = NavidromeNative("http://nd", "u", "p", client=client)
        assert await native.list_albums() == []


class TestSyncPass:
    async def test_copies_mbids_onto_existing_rows(self, db):
        db.add(Album(id="al1", name="Album One", artist_name="Artist One"))
        db.commit()

        native = NavidromeNative("http://nd", "u", "p", client=FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [album_row()])],
        ))
        stats = await sync_album_mbids(db, native)

        assert stats["tagged"] == 1
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid == RG
        assert album.mbz_album_id == REL
        assert album.mbid_source == "navidrome_tag"
        assert album.mbid_confidence == "high"
        assert album.mbid_checked_at is not None

    async def test_untagged_rows_are_left_alone(self, db):
        db.add(Album(id="al1", name="No Tags", mbid_source=None))
        db.commit()
        native = NavidromeNative("http://nd", "u", "p", client=FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [album_row(mbzReleaseGroupId=None)])],
        ))
        stats = await sync_album_mbids(db, native)
        assert stats["tagged"] == 0
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid is None
        assert album.mbid_checked_at is None  # still "never attempted"

    async def test_unknown_navidrome_album_does_not_create_rows(self, db):
        native = NavidromeNative("http://nd", "u", "p", client=FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [album_row(id="ghost")])],
        ))
        stats = await sync_album_mbids(db, native)
        assert stats["missing_rows"] == 1
        assert db.query(Album).count() == 0

    async def test_does_not_downgrade_a_validated_oracle_id(self, db):
        db.add(Album(id="al1", name="Album One",
                     release_group_mbid="oracle-id",
                     mbid_source="lidarr_oracle", mbid_confidence="high"))
        db.commit()
        native = NavidromeNative("http://nd", "u", "p", client=FakeClient(
            posts=[FakeResponse(200, {"token": "t"})],
            gets=[FakeResponse(200, [album_row()])],
        ))
        await sync_album_mbids(db, native)
        album = db.query(Album).filter_by(id="al1").one()
        # The tag is still the better authority for the id itself...
        assert album.release_group_mbid == RG
        # ...but we do not rewrite provenance for an id we already trusted.
        assert album.mbid_source == "lidarr_oracle"

    async def test_failure_is_swallowed(self, db):
        class BoomNative:
            async def iter_albums(self, page_size=200):
                raise RuntimeError("navidrome down")
                yield  # pragma: no cover

        stats = await sync_album_mbids(db, BoomNative())
        assert stats.get("error")
        assert stats["tagged"] == 0
