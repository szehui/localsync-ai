"""Blocklist: it must be enforced, not merely hidden.

Four behaviours are load-bearing and each is pinned here:

* a blocked album is refused **at creation**, so a client that never saw a
  filtered search cannot request it anyway;
* a blocked album is **filtered out of search and rails**, so the household is
  not offered what it already rejected;
* and **still listed in an artist's discography**, flagged — an album quietly
  missing from a discography reads as a bug, not a policy;
* only the owner may read or write the list, because the reasons are the
  owner's notes and a member who could edit it could hide albums from everyone.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Album,
    Base,
    Blocklist,
    Request,
    User,
)
from app.services import blocklist as bl
from app.services import catalog
from app.services import requests as svc

RG = "11111111-1111-1111-1111-111111111111"
OTHER_RG = "22222222-2222-2222-2222-222222222222"
ARTIST = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def owner(db):
    user = User(username="sze", display_name="Sze", navidrome_url="http://nav",
                navidrome_password="x", is_owner=True)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def guest(db):
    user = User(username="rebecca", display_name="Rebecca",
                navidrome_url="http://nav", navidrome_password="x")
    db.add(user)
    db.commit()
    return user


def as_dict(user):
    return {"id": user.id, "username": user.username,
            "navidrome_url": user.navidrome_url}


def rg_payload(mbid=RG, title="Random Access Memories", artist="Daft Punk",
               artist_id=ARTIST, primary="Album"):
    return {
        "id": mbid,
        "title": title,
        "primary-type": primary,
        "secondary-types": [],
        "first-release-date": "2013-05-17",
        "artist-credit": [{"name": artist, "artist": {"id": artist_id}}],
    }


# ── Service: CRUD and permissions ───────────────────────────────────────────


class TestAddRemove:
    def test_album_block_round_trip(self, db, owner):
        row = bl.add(db, as_dict(owner), scope="release_group",
                     release_group_mbid=RG, artist_name="Daft Punk",
                     album_title="Random Access Memories", reason="not for us")
        assert row.id is not None
        entry = bl.to_entry(db, row)
        assert entry["scope"] == "release_group"
        assert entry["album_title"] == "Random Access Memories"
        assert entry["created_by"] == "Sze"
        assert entry["reason"] == "not for us"

    def test_artist_block_stores_only_the_artist_id(self, db, owner):
        row = bl.add(db, as_dict(owner), scope="artist", artist_mbid=ARTIST,
                     artist_name="Daft Punk")
        assert row.artist_mbid == ARTIST
        assert row.release_group_mbid is None

    def test_member_cannot_block(self, db, owner, guest):
        with pytest.raises(bl.BlocklistError) as exc:
            bl.add(db, as_dict(guest), scope="release_group",
                   release_group_mbid=RG)
        assert exc.value.status == 403

    def test_member_cannot_unblock(self, db, owner, guest):
        row = bl.add(db, as_dict(owner), scope="release_group",
                     release_group_mbid=RG)
        with pytest.raises(bl.BlocklistError) as exc:
            bl.remove(db, as_dict(guest), row.id)
        assert exc.value.status == 403

    def test_duplicate_is_refused(self, db, owner):
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)
        with pytest.raises(bl.BlocklistError) as exc:
            bl.add(db, as_dict(owner), scope="release_group",
                   release_group_mbid=RG)
        assert exc.value.status == 409

    def test_missing_id_is_refused(self, db, owner):
        with pytest.raises(bl.BlocklistError) as exc:
            bl.add(db, as_dict(owner), scope="artist")
        assert exc.value.code == "missing_mbid"

    def test_unknown_scope_is_refused(self, db, owner):
        with pytest.raises(bl.BlocklistError) as exc:
            bl.add(db, as_dict(owner), scope="label",
                   release_group_mbid=RG)
        assert exc.value.code == "bad_scope"

    def test_remove_is_a_real_delete(self, db, owner):
        row = bl.add(db, as_dict(owner), scope="release_group",
                     release_group_mbid=RG)
        bl.remove(db, as_dict(owner), row.id)
        assert db.query(Blocklist).count() == 0

    def test_remove_unknown_is_404(self, db, owner):
        with pytest.raises(bl.BlocklistError) as exc:
            bl.remove(db, as_dict(owner), 999)
        assert exc.value.status == 404


# ── Enforcement in the request write-path ───────────────────────────────────


class TestEnforcement:
    async def test_blocked_album_cannot_be_requested(self, db, owner):
        """A client that never saw a filtered search is still refused."""
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)

        client = MagicMock()
        client.get_release_group = AsyncMock(return_value=rg_payload())
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG, mb=client)
        assert exc.value.code == "blocked"
        assert exc.value.status == 403
        assert db.query(Request).count() == 0

    async def test_blocking_the_artist_blocks_every_album(self, db, owner):
        bl.add(db, as_dict(owner), scope="artist", artist_mbid=ARTIST)

        client = MagicMock()
        client.get_release_group = AsyncMock(return_value=rg_payload())
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG, mb=client)
        assert exc.value.code == "blocked"

    async def test_blocked_artist_cannot_be_requested_as_a_discography(self, db, owner):
        bl.add(db, as_dict(owner), scope="artist", artist_mbid=ARTIST)
        client = MagicMock()
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_artist_request(db, as_dict(owner), ARTIST, mb=client)
        assert exc.value.code == "blocked"

    async def test_plan_skips_a_blocked_album_in_a_discography(self, db, owner):
        """Blocking one album must not make the whole artist unrequestable."""
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=OTHER_RG)

        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": ARTIST, "name": "Daft Punk"})
        client.browse_artist_release_groups = AsyncMock(return_value=[
            rg_payload(mbid=RG, title="Homework"),
            rg_payload(mbid=OTHER_RG, title="Discovery"),
        ])
        client.close = AsyncMock()

        plan = await svc.plan_artist_request(db, ARTIST, mb=client)
        assert [a["release_group_mbid"] for a in plan["albums"]] == [RG]
        assert plan["summary"]["blocked"] == 1
        assert plan["summary"]["requestable"] == 1

    async def test_a_block_does_not_cancel_an_existing_request(self, db, owner):
        """Blocking is forward-looking; killing in-flight work is a separate call."""
        request = Request(user_id=owner.id, kind="album", release_group_mbid=RG,
                          artist_name="Daft Punk", album_title="RAM",
                          status="approved")
        db.add(request)
        db.commit()

        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)

        db.refresh(request)
        assert request.status == "approved"


# ── Enforcement in the catalog ──────────────────────────────────────────────


class TestCatalogFiltering:
    def test_annotate_flags_blocked_cards(self, db, owner):
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)
        cards = catalog.annotate(db, [rg_payload(), rg_payload(mbid=OTHER_RG)])
        flags = {c["release_group_mbid"]: c["blocked"] for c in cards}
        assert flags[RG] is True
        assert flags[OTHER_RG] is False

    def test_artist_block_flags_every_card_by_that_artist(self, db, owner):
        bl.add(db, as_dict(owner), scope="artist", artist_mbid=ARTIST)
        cards = catalog.annotate(db, [rg_payload(), rg_payload(mbid=OTHER_RG,
                                                              title="Discovery")])
        assert all(c["blocked"] for c in cards)

    def test_filter_blocked_drops_both_scopes(self, db, owner):
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)
        bl.add(db, as_dict(owner), scope="artist", artist_mbid="artist-2")
        cards = catalog.annotate(db, [
            rg_payload(),
            rg_payload(mbid=OTHER_RG, artist="Other", artist_id="artist-2"),
            rg_payload(mbid="rg-3", title="Kept", artist="Kept", artist_id="artist-3"),
        ])
        kept = bl.filter_blocked(db, cards)
        assert [c["release_group_mbid"] for c in kept] == ["rg-3"]

    def test_no_blocks_means_no_filtering(self, db, owner):
        cards = catalog.annotate(db, [rg_payload(), rg_payload(mbid=OTHER_RG)])
        assert bl.filter_blocked(db, cards) == cards

    async def test_search_hides_a_blocked_album(self, db, owner):
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)

        client = MagicMock()
        client.search_release_groups = AsyncMock(return_value={
            "release_groups": [rg_payload(), rg_payload(mbid=OTHER_RG,
                                                        title="Discovery")],
            "count": 2,
        })
        client.search_artists = AsyncMock(return_value=[])
        client.close = AsyncMock()

        result = await catalog.search(db, as_dict(owner), "daft punk", mb=client)
        assert [c["release_group_mbid"] for c in result["results"]] == [OTHER_RG]

    async def test_artist_page_keeps_a_blocked_album_but_flags_it(self, db, owner):
        """Silently dropping it from the discography would read as a bug."""
        bl.add(db, as_dict(owner), scope="release_group", release_group_mbid=RG)

        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": ARTIST, "name": "Daft Punk"})
        client.browse_artist_release_groups = AsyncMock(return_value=[
            rg_payload(mbid=RG, title="Homework"),
            rg_payload(mbid=OTHER_RG, title="Discovery"),
        ])
        client.close = AsyncMock()

        detail = await catalog.artist_detail(db, as_dict(owner), ARTIST, mb=client)
        flags = {a["release_group_mbid"]: a["blocked"] for a in detail["albums"]}
        assert flags == {RG: True, OTHER_RG: False}
