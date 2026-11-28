"""Tests for the catalog service: one card shape, ownership, requests."""
import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Album, Base, Request, User
from app.services import catalog
from app.services.catalog import annotate, card, ownership_index

MBID = "aaaaaaaa-0000-0000-0000-000000000001"
MBID2 = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    row = User(id=1, username="sze", is_owner=True,
               navidrome_url="http://localhost:4535", navidrome_password="x")
    db.add(row)
    db.commit()
    return row


def rg(title="Hysteria", artist="Def Leppard", mbid=MBID, artist_id="artist-1",
       **extra):
    return {"id": mbid, "title": title, "primary-type": "Album",
            "secondary-types": [], "first-release-date": "1987-08-03",
            "score": 100,
            "artist-credit": [{"name": artist, "artist": {"id": artist_id}}],
            **extra}


class TestCard:
    def test_shape_has_every_documented_field(self, db):
        out = card(rg())
        assert set(out) == {
            "release_group_mbid", "title", "artist_name", "artist_mbid", "year",
            "primary_type", "secondary_types", "cover_url", "score",
            "owned", "owned_via", "request",
        }
        assert out["title"] == "Hysteria"
        assert out["artist_name"] == "Def Leppard"
        assert out["year"] == 1987
        assert out["owned"] is False

    def test_year_from_first_release_date(self, db):
        assert card(rg(**{"first-release-date": "1991"}))["year"] == 1991

    def test_missing_date_yields_none(self, db):
        assert card({"id": MBID, "title": "X"})["year"] is None


class TestOwnership:
    def test_mbid_beats_name(self, db):
        db.add(Album(id="al1", name="Something Entirely Different",
                     artist_name="Def Leppard", release_group_mbid=MBID))
        db.commit()
        index = ownership_index(db)
        assert index.lookup(MBID, "Def Leppard", "Hysteria") == (True, "mbid")

    def test_name_match_is_reported_as_name(self, db):
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard"))
        db.commit()
        index = ownership_index(db)
        assert index.lookup(MBID, "Def Leppard", "Hysteria") == (True, "name")

    def test_name_match_folds(self, db):
        db.add(Album(id="al1", name="Fire & Fury", artist_name="Sam and Dave"))
        db.commit()
        assert ownership_index(db).lookup(MBID, "Sam & Dave", "Fire and Fury")[0] is True

    def test_untagged_rip_yields_probably(self, db):
        # A single-track rip with a name is the floor case: name match, no MBID.
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard",
                     song_count=1))
        db.commit()
        cards = annotate(db, [rg()])
        assert cards[0]["owned"] is True
        assert cards[0]["owned_via"] == "name"

    def test_not_owned(self, db):
        db.add(Album(id="al1", name="Adrenalize", artist_name="Def Leppard"))
        db.commit()
        cards = annotate(db, [rg()])
        assert cards[0]["owned"] is False
        assert cards[0]["owned_via"] is None

    def test_ownership_precedence_in_cards(self, db):
        # Both an MBID row and a name row exist: MBID wins.
        db.add_all([
            Album(id="al1", name="Hysteria", artist_name="Def Leppard"),
            Album(id="al2", name="Unrelated", artist_name="Unrelated Artist",
                  release_group_mbid=MBID),
        ])
        db.commit()
        assert annotate(db, [rg()])[0]["owned_via"] == "mbid"


class TestRequests:
    def test_open_request_is_annotated(self, db, user):
        db.add(Album(id="al1", name="X", artist_name="Y"))
        db.commit()
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="pending"))
        db.commit()
        cards = annotate(db, [rg()])
        assert cards[0]["request"] == {
            "id": cards[0]["request"]["id"], "status": "pending",
            "requested_by": "sze",
        }

    def test_denied_request_does_not_block(self, db, user):
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="denied"))
        db.commit()
        assert annotate(db, [rg()])[0]["request"] is None

    def test_two_open_requests_for_one_album_are_impossible(self, db, user):
        # The partial unique index is the concurrency guard: two users clicking
        # request at once cannot create two rows. Voting is how the second user
        # joins (Phase 2), not a duplicate request.
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="pending"))
        db.commit()
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="pending"))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

    def test_denied_then_re_request_is_allowed(self, db, user):
        # Denied rows are excluded from the partial index, so a user can ask again.
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="denied"))
        db.commit()
        db.add(Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                       status="pending"))
        db.commit()
        assert db.query(Request).filter_by(release_group_mbid=MBID).count() == 2


class TestAnnotate:
    def test_request_lookup_is_batched(self, db, user):
        db.add_all([
            Request(user_id=user.id, kind="album", release_group_mbid=MBID,
                    status="pending"),
            Request(user_id=user.id, kind="album", release_group_mbid=MBID2,
                    status="pending"),
        ])
        db.commit()
        cards = annotate(db, [rg(), rg(title="Other", mbid=MBID2)])
        assert [c["request"] is not None for c in cards] == [True, True]

    def test_no_albums_still_annotates(self, db):
        cards = annotate(db, [rg()])
        assert cards[0]["owned"] is False


# ── Service-level tests with a scripted MusicBrainz client ──────────────────


class FakeMB:
    def __init__(self, search=None, detail=None, artist=None, browse=None,
                 artists=None, count=0):
        self._search = search or []
        self._detail = detail
        self._artist = artist
        self._browse = browse or {}
        self._artists = artists or []
        self.count = count
        self.search_calls = []
        self.closed = False

    async def search_release_groups(self, query, *, limit=10, offset=0):
        self.search_calls.append((query, limit, offset))
        return {"release_groups": self._search, "count": self.count or len(self._search)}

    async def get_release_group(self, mbid, *, inc="artist-credits"):
        return self._detail

    async def get_artist(self, mbid, *, inc="url-rels"):
        return self._artist

    async def browse_artist_release_groups(self, artist_mbid, *, type_="album",
                                           limit=100, offset=0):
        return self._browse.get(type_, [])

    async def search_artists(self, query, *, limit=5):
        return self._artists

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_cover_network(monkeypatch):
    """Artwork is a separate unit; these tests must not touch the network."""
    async def _none(db, ids, **kwargs):
        return {i: None for i in ids}
    monkeypatch.setattr("app.services.catalog.front_covers", _none)


class TestSearchService:
    async def test_returns_annotated_cards(self, db, user):
        mb = FakeMB(search=[rg()])
        out = await catalog.search(db, {"id": 1}, "hysteria", mb=mb)
        assert out["results"][0]["title"] == "Hysteria"
        assert out["results"][0]["owned"] is False
        assert out["count"] == 1

    async def test_empty_query_makes_no_upstream_call(self, db, user):
        mb = FakeMB(search=[rg()])
        out = await catalog.search(db, {"id": 1}, "   ", mb=mb)
        assert out["results"] == []
        assert mb.search_calls == []

    async def test_no_results_offers_artist_suggestions(self, db, user):
        mb = FakeMB(search=[], artists=[{"id": "a1", "name": "Def Leppard",
                                         "disambiguation": "UK rock band"}])
        out = await catalog.search(db, {"id": 1}, "def leppard", mb=mb)
        assert out["results"] == []
        assert out["artist_suggestions"] == [
            {"artist_mbid": "a1", "artist_name": "Def Leppard",
             "disambiguation": "UK rock band"}]

    async def test_injects_a_client_it_created_and_closes_it(self, db, user):
        # When no client is injected, search must own (and close) its own.
        created = FakeMB(search=[rg()])

        class Factory(FakeMB):
            def __init__(self, **kw):
                super().__init__(**kw)

        with patch("app.services.catalog.MusicBrainzClient", return_value=created):
            await catalog.search(db, {"id": 1}, "hysteria")
        assert created.closed is True

    async def test_injected_client_is_not_closed(self, db, user):
        mb = FakeMB(search=[rg()])
        await catalog.search(db, {"id": 1}, "hysteria", mb=mb)
        assert mb.closed is False

    async def test_artist_suggestions_are_offered_even_with_hits(self, db, user):
        # Searching an artist name returns albums that only share the word, so
        # the artist strip cannot be reserved for the empty-result case.
        mb = FakeMB(search=[rg()], artists=[{"id": "a1", "name": "TWICE"}])
        out = await catalog.search(db, {"id": 1}, "twice", mb=mb)
        assert out["results"], "precondition: there are album hits"
        assert out["artist_suggestions"] == [
            {"artist_mbid": "a1", "artist_name": "TWICE", "disambiguation": None}]

    async def test_repeat_search_is_served_from_cache(self, db, user):
        mb = FakeMB(search=[rg()])
        await catalog.search(db, {"id": 1}, "hysteria", mb=mb)
        again = FakeMB(search=[rg(title="Should Not Appear")])
        out = await catalog.search(db, {"id": 1}, "hysteria", mb=again)
        assert again.search_calls == [], "a repeat query must not spend a request"
        assert out["results"][0]["title"] == "Hysteria"

    async def test_cached_search_still_recomputes_ownership(self, db, user):
        # Upstream payloads are cached; ownership is not. Otherwise an album
        # imported during the cache window keeps offering "Request".
        mb = FakeMB(search=[rg()])
        first = await catalog.search(db, {"id": 1}, "hysteria", mb=mb)
        assert first["results"][0]["owned"] is False

        album = Album(
            id="lib-1", name="Hysteria", artist_name="Def Leppard", song_count=12,
            release_group_mbid=first["results"][0]["release_group_mbid"],
        )
        db.add(album)
        db.commit()

        again = await catalog.search(db, {"id": 1}, "hysteria", mb=FakeMB())
        assert again["results"][0]["owned"] is True


class TestAlbumDetail:
    async def test_records_aliases_on_first_view(self, db, user):
        detail = rg(releases=[{"id": "r1", "title": "PSY 9th"}])
        detail["title"] = "싸다9"
        mb = FakeMB(detail=detail)
        out = await catalog.album_detail(db, {"id": 1}, MBID, mb=mb)
        assert out["title"] == "싸다9"
        assert out["aliases"] == ["PSY 9th"]

    async def test_missing_release_group_returns_none(self, db, user):
        mb = FakeMB(detail=None)
        assert await catalog.album_detail(db, {"id": 1}, MBID, mb=mb) is None

    async def test_includes_requestable_and_releases(self, db, user):
        detail = rg(releases=[{"id": "r1", "title": "Hysteria", "date": "1987"}])
        out = await catalog.album_detail(db, {"id": 1}, MBID, mb=FakeMB(detail=detail))
        assert out["requestable"] is True
        assert out["releases"][0]["id"] == "r1"


class TestArtistDetail:
    async def test_summary_counts_owned_and_requestable(self, db, user):
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard"))
        db.commit()
        mb = FakeMB(
            artist={"id": "artist-1", "name": "Def Leppard"},
            browse={"album": [rg(), rg(title="Adrenalize")], "ep": []},
        )
        out = await catalog.artist_detail(db, {"id": 1}, "artist-1", mb=mb)
        assert out["summary"]["albums"] == 2
        assert out["summary"]["owned"] == 1
        assert out["summary"]["requestable"] == 1

    async def test_missing_artist_returns_none(self, db, user):
        assert await catalog.artist_detail(db, {"id": 1}, "x",
                                           mb=FakeMB(artist=None)) is None
