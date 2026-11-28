"""Request service: creation, dedupe-as-vote, and the approval lifecycle.

The behaviours pinned here are the ones that cost real downloads when wrong:

* a second ask must become a vote, not a second Lidarr add;
* a non-owner must not be able to approve;
* a transient Lidarr failure must stay retryable, not terminal;
* a cancelled or denied request must free the release group for a later ask.
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
    RequestVote,
    User,
)
from app.services import requests as svc


RG = "11111111-1111-1111-1111-111111111111"
OTHER_RG = "22222222-2222-2222-2222-222222222222"


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
    user = User(username="sze", display_name="sze", navidrome_url="http://nav",
                navidrome_password="x", is_owner=True)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def guest(db):
    user = User(username="rebecca", display_name="rebecca", navidrome_url="http://nav",
                navidrome_password="x")
    db.add(user)
    db.commit()
    return user


def as_dict(user):
    return {"id": user.id, "username": user.username, "navidrome_url": user.navidrome_url}


def rg_payload(mbid=RG, title="Random Access Memories", primary="Album",
               secondary=None, artist="Daft Punk", artist_id="artist-1"):
    return {
        "id": mbid,
        "title": title,
        "primary-type": primary,
        "secondary-types": secondary or [],
        "first-release-date": "2013-05-17",
        "artist-credit": [{"name": artist, "artist": {"id": artist_id}}],
    }


@pytest.fixture
def mb(monkeypatch):
    """A MusicBrainz client that knows one album and one non-requestable type."""
    client = MagicMock()
    client.get_release_group = AsyncMock(return_value=rg_payload())
    client.get_artist = AsyncMock(return_value={"id": "artist-1", "name": "Daft Punk"})
    client.browse_artist_release_groups = AsyncMock(return_value=[rg_payload()])
    client.close = AsyncMock()

    async def fake_fetch(db, mbid, *, mb=None):
        return rg_payload(mbid=mbid)

    monkeypatch.setattr(svc, "fetch_release_group", fake_fetch)
    monkeypatch.setattr(svc, "_cover", AsyncMock(return_value=None))
    return client


@pytest.fixture
def lidarr(monkeypatch):
    """A Lidarr service whose push outcome the test controls."""
    service = MagicMock()
    service.add_albums_by_mbid = AsyncMock(return_value={
        "added": [{"release_group_mbid": RG, "album_id": 10, "artist_id": 1}],
        "already_present": [], "unresolved": [], "failed": [],
        "monitoring_ok": True,
        "results": [{"release_group_mbid": RG, "album_id": 10, "artist_id": 1}],
    })
    monkeypatch.setattr(svc, "_lidarr_service", lambda db: service)
    return service


# ── Creation ────────────────────────────────────────────────────────────────


class TestCreateAlbumRequest:
    async def test_creates_a_pending_request_with_display_fields(self, db, owner, mb):
        result = await svc.create_album_request(db, as_dict(owner), RG)

        assert result["created"] is True
        request = result["request"]
        assert request.status == "pending"      # auto_approve defaults false
        assert request.album_title == "Random Access Memories"
        assert request.artist_name == "Daft Punk"
        assert request.release_group_mbid == RG

    async def test_requester_is_implicitly_a_voter(self, db, owner, mb):
        result = await svc.create_album_request(db, as_dict(owner), RG)
        assert svc.vote_count(db, result["request"].id) == 1

    async def test_second_requester_becomes_a_vote(self, db, owner, guest, mb):
        """The whole dedupe contract: one row, two votes, one Lidarr add."""
        first = await svc.create_album_request(db, as_dict(owner), RG)
        second = await svc.create_album_request(db, as_dict(guest), RG)

        assert second["created"] is False
        assert second["voted"] is True
        assert second["request"].id == first["request"].id
        assert db.query(Request).count() == 1
        assert svc.vote_count(db, first["request"].id) == 2

    async def test_same_user_asking_twice_does_not_double_vote(self, db, owner, mb):
        first = await svc.create_album_request(db, as_dict(owner), RG)
        again = await svc.create_album_request(db, as_dict(owner), RG)

        assert again["created"] is False
        assert again["voted"] is False        # idempotent vote
        assert svc.vote_count(db, first["request"].id) == 1

    async def test_denied_request_can_be_asked_for_again(self, db, owner, guest, mb, lidarr):
        first = await svc.create_album_request(db, as_dict(guest), RG)
        svc.deny(db, first["request"], as_dict(owner), reason="not this one")

        again = await svc.create_album_request(db, as_dict(guest), RG)
        assert again["created"] is True
        assert again["request"].id != first["request"].id

    async def test_musicbrainz_400_means_not_found(self, db, owner):
        """A well-formed but nonexistent UUID is a 404 for us, not a 500.

        MusicBrainz answers 400 (not 404) for `release-group/<valid-uuid>` when
        it has never seen that id, so the client's allow_404 flag does not cover
        it. Found live: the endpoint returned an HTML 500 page on a bogus MBID.
        """
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        response = httpx.Response(400, request=httpx.Request(
            "GET", "https://musicbrainz.org/ws/2/release-group/x"))
        client = MagicMock()
        client.get_release_group = AsyncMock(
            side_effect=httpx.HTTPStatusError("400", request=response.request,
                                              response=response))
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG, mb=client)
        assert exc.value.code == "not_found"
        assert exc.value.status == 404
        assert db.query(Request).count() == 0

    async def test_musicbrainz_outage_is_a_503_not_a_failure(self, db, owner):
        """A 5xx upstream is 'try again', not 'this album does not exist'."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        response = httpx.Response(503, request=httpx.Request(
            "GET", "https://musicbrainz.org/ws/2/release-group/x"))
        client = MagicMock()
        client.get_release_group = AsyncMock(
            side_effect=httpx.HTTPStatusError("503", request=response.request,
                                              response=response))
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG, mb=client)
        assert exc.value.code == "upstream"
        assert exc.value.status == 503

    async def test_blocklisted_album_is_refused(self, db, owner, mb):
        db.add(Blocklist(scope="release_group", release_group_mbid=RG,
                         reason="no", created_by=owner.id))
        db.commit()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG)
        assert exc.value.code == "blocked"
        assert exc.value.status == 403

    async def test_blocklisted_artist_is_refused(self, db, owner, mb):
        db.add(Blocklist(scope="artist", artist_mbid="artist-1",
                         reason="no", created_by=owner.id))
        db.commit()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG)
        assert exc.value.code == "blocked"

    async def test_owned_album_is_refused(self, db, owner, mb):
        db.add(Album(id="a1", name="Random Access Memories",
                     artist_name="Daft Punk", release_group_mbid=RG))
        db.commit()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG)
        assert exc.value.code == "already_owned"
        assert exc.value.status == 409

    async def test_name_match_alone_does_not_block_a_request(self, db, owner, mb):
        """An untagged rip of a same-titled album must not refuse the ask.

        Only the MBID tier is authoritative at creation; the name tier is a
        "probably in your library" hint. Blocking on it would refuse live
        albums, remixes and unrelated same-named releases.
        """
        db.add(Album(id="a1", name="Random Access Memories",
                     artist_name="Daft Punk", release_group_mbid=None))
        db.commit()

        result = await svc.create_album_request(db, as_dict(owner), RG)
        assert result["created"] is True

    async def test_user_without_permission_is_refused(self, db, guest, mb):
        guest.can_request = False
        db.commit()

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(guest), RG)
        assert exc.value.code == "cannot_request"
        assert exc.value.status == 403

    async def test_non_requestable_type_is_refused(self, db, owner, mb, monkeypatch):
        async def fake_fetch(db_, mbid, *, mb=None):
            return rg_payload(primary="Single")

        monkeypatch.setattr(svc, "fetch_release_group", fake_fetch)

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_album_request(db, as_dict(owner), RG)
        assert exc.value.code == "not_requestable"

    async def test_auto_approve_pushes_immediately(self, db, guest, mb, lidarr):
        guest.auto_approve = True
        db.commit()

        result = await svc.create_album_request(db, as_dict(guest), RG)

        assert result["request"].status == "approved"
        assert result["request"].lidarr_album_id == 10
        lidarr.add_albums_by_mbid.assert_awaited_once_with([RG])


# ── Approval lifecycle ──────────────────────────────────────────────────────


class TestLifecycle:
    async def test_owner_can_approve_and_records_lidarr_ids(self, db, owner, guest,
                                                             mb, lidarr):
        created = await svc.create_album_request(db, as_dict(guest), RG)
        request = await svc.approve(db, created["request"], as_dict(owner))

        assert request.status == "approved"
        assert request.lidarr_album_id == 10
        assert request.lidarr_artist_id == 1
        assert request.decided_by == owner.id
        assert request.last_error is None

    async def test_non_owner_without_auto_approve_cannot_approve(self, db, owner,
                                                                 guest, mb, lidarr):
        created = await svc.create_album_request(db, as_dict(owner), RG)

        with pytest.raises(svc.RequestError) as exc:
            await svc.approve(db, created["request"], as_dict(guest))
        assert exc.value.code == "forbidden"
        assert exc.value.status == 403
        lidarr.add_albums_by_mbid.assert_not_awaited()

    async def test_auto_approved_user_can_approve_their_own_request(self, db, owner,
                                                                   guest, mb, lidarr):
        created = await svc.create_album_request(db, as_dict(guest), RG)
        guest.auto_approve = True
        db.commit()

        request = await svc.approve(db, created["request"], as_dict(guest))
        assert request.status == "approved"

    async def test_auto_approved_user_cannot_approve_someone_elses(self, db, owner,
                                                                  guest, mb, lidarr):
        created = await svc.create_album_request(db, as_dict(owner), RG)
        guest.auto_approve = True
        db.commit()

        with pytest.raises(svc.RequestError):
            await svc.approve(db, created["request"], as_dict(guest))

    async def test_transport_failure_stays_approved_and_retryable(self, db, owner,
                                                                  mb, lidarr):
        """A Lidarr 503 is throttling, not a verdict: never mark it failed."""
        lidarr.add_albums_by_mbid = AsyncMock(side_effect=RuntimeError("503 busy"))
        created = await svc.create_album_request(db, as_dict(owner), RG)
        request = await svc.approve(db, created["request"], as_dict(owner))

        assert request.status == "approved"
        assert "busy" in request.last_error

        # Retry after the upstream recovers clears the error.
        lidarr.add_albums_by_mbid = AsyncMock(return_value={
            "results": [{"release_group_mbid": RG, "album_id": 11, "artist_id": 1}],
            "added": [], "already_present": [], "unresolved": [], "failed": [],
            "monitoring_ok": True,
        })
        request = await svc.retry(db, request, as_dict(owner))
        assert request.last_error is None
        assert request.lidarr_album_id == 11

    async def test_unresolved_mbid_records_the_reason(self, db, owner, mb, lidarr):
        lidarr.add_albums_by_mbid = AsyncMock(return_value={
            "results": [{"release_group_mbid": RG,
                         "error": f"Lidarr has no album for release group {RG}"}],
            "added": [], "already_present": [], "failed": [], "monitoring_ok": True,
            "unresolved": [{"release_group_mbid": RG,
                            "error": "no album for release group"}],
        })
        created = await svc.create_album_request(db, as_dict(owner), RG)
        request = await svc.approve(db, created["request"], as_dict(owner))

        assert "no album for release group" in request.last_error
        assert request.lidarr_album_id is None

    async def test_deny_is_owner_only_and_records_reason(self, db, owner, guest, mb):
        created = await svc.create_album_request(db, as_dict(guest), RG)

        with pytest.raises(svc.RequestError):
            svc.deny(db, created["request"], as_dict(guest), reason="nope")

        request = svc.deny(db, created["request"], as_dict(owner), reason="kids' music")
        assert request.status == "denied"
        assert "kids' music" in request.note

    async def test_cancel_allowed_for_requester_while_pending(self, db, owner, guest, mb):
        created = await svc.create_album_request(db, as_dict(guest), RG)
        request = svc.cancel(db, created["request"], as_dict(guest))
        assert request.status == "cancelled"

    async def test_cancel_refused_after_approval(self, db, owner, guest, mb, lidarr):
        """Cancelling an approved request would leave an untracked album in Lidarr."""
        created = await svc.create_album_request(db, as_dict(guest), RG)
        svc.approve  # approved path is exercised elsewhere
        approved = await svc.approve(db, created["request"], as_dict(owner))

        with pytest.raises(svc.RequestError) as exc:
            svc.cancel(db, approved, as_dict(guest))
        assert exc.value.code == "not_pending"

    async def test_retry_is_owner_only(self, db, owner, guest, mb, lidarr):
        created = await svc.create_album_request(db, as_dict(guest), RG)
        created["request"].status = "approved"
        created["request"].last_error = "boom"
        db.commit()

        with pytest.raises(svc.RequestError):
            await svc.retry(db, created["request"], as_dict(guest))

    async def test_approve_refuses_a_closed_request(self, db, owner, guest, mb, lidarr):
        created = await svc.create_album_request(db, as_dict(guest), RG)
        svc.deny(db, created["request"], as_dict(owner))

        with pytest.raises(svc.RequestError) as exc:
            await svc.approve(db, created["request"], as_dict(owner))
        assert exc.value.code == "not_open"


# ── Artist requests ─────────────────────────────────────────────────────────


class TestArtistRequests:
    async def test_expansion_skips_owned_and_already_requested(self, db, owner,
                                                               monkeypatch):
        db.add(Album(id="a1", name="Homework", artist_name="Daft Punk",
                     release_group_mbid="rg-owned"))
        db.commit()
        # An existing open request for another album in the discography.
        db.add(Request(user_id=owner.id, kind="album", release_group_mbid="rg-pending",
                       album_title="Discovery", status="pending"))
        db.commit()

        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": "artist-1",
                                                    "name": "Daft Punk"})
        client.browse_artist_release_groups = AsyncMock(return_value=[
            rg_payload(mbid="rg-owned", title="Homework"),
            rg_payload(mbid="rg-pending", title="Discovery"),
            rg_payload(mbid="rg-new", title="Random Access Memories"),
        ])
        client.close = AsyncMock()

        plan = await svc.plan_artist_request(db, "artist-1", mb=client)

        assert [a["release_group_mbid"] for a in plan["albums"]] == ["rg-new"]
        assert plan["summary"] == {"requestable": 1, "owned": 1,
                                   "already_requested": 1, "blocked": 0,
                                   "total": 3}

    async def test_studio_scope_excludes_soundtracks_and_compilations(self, db, owner):
        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": "artist-1", "name": "X"})
        client.browse_artist_release_groups = AsyncMock(return_value=[
            rg_payload(mbid="rg-studio", title="Album"),
            rg_payload(mbid="rg-soundtrack", title="OST", secondary=["Soundtrack"]),
            rg_payload(mbid="rg-comp", title="Greatest", secondary=["Compilation"]),
        ])
        client.close = AsyncMock()

        plan = await svc.plan_artist_request(db, "artist-1", scope="studio", mb=client)
        assert [a["release_group_mbid"] for a in plan["albums"]] == ["rg-studio"]

    async def test_studio_scope_browses_album_type_only(self, db, owner):
        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": "artist-1", "name": "X"})
        client.browse_artist_release_groups = AsyncMock(return_value=[])
        client.close = AsyncMock()

        await svc.plan_artist_request(db, "artist-1", scope="studio", mb=client)
        # One type per call: repeating `type` makes MusicBrainz 503 (Phase 0).
        types = [c.kwargs.get("type_") for c in
                 client.browse_artist_release_groups.await_args_list]
        assert types == ["album"]

    async def test_studio_eps_browses_both_types_separately(self, db, owner):
        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": "artist-1", "name": "X"})
        client.browse_artist_release_groups = AsyncMock(return_value=[])
        client.close = AsyncMock()

        await svc.plan_artist_request(db, "artist-1", scope="studio_eps", mb=client)
        types = [c.kwargs.get("type_") for c in
                 client.browse_artist_release_groups.await_args_list]
        assert types == ["album", "ep"]

    async def test_cap_refuses_an_absurd_expansion(self, db, owner, monkeypatch):
        monkeypatch.setattr(svc, "MAX_ARTIST_ALBUMS", 2)
        client = MagicMock()
        client.get_artist = AsyncMock(return_value={"id": "artist-1", "name": "X"})
        client.browse_artist_release_groups = AsyncMock(return_value=[
            rg_payload(mbid=f"rg-{i}", title=f"Album {i}") for i in range(5)
        ])
        client.close = AsyncMock()

        with pytest.raises(svc.RequestError) as exc:
            await svc.plan_artist_request(db, "artist-1", mb=client)
        assert exc.value.code == "too_many"

    async def test_creation_makes_children_and_rolls_up(self, db, owner, monkeypatch):
        async def fake_plan(db_, artist_mbid, *, scope="studio", mb=None):
            return {
                "artist_mbid": "artist-1", "artist_name": "Daft Punk",
                "disambiguation": None, "scope": scope,
                "albums": [
                    {"release_group_mbid": "rg-a", "title": "A", "year": 2001,
                     "primary_type": "Album"},
                    {"release_group_mbid": "rg-b", "title": "B", "year": 2005,
                     "primary_type": "Album"},
                ],
                "summary": {"requestable": 2, "owned": 0, "already_requested": 0,
                            "total": 2},
            }

        monkeypatch.setattr(svc, "plan_artist_request", fake_plan)

        result = await svc.create_artist_request(db, as_dict(owner), "artist-1")
        parent = result["request"]

        assert parent.kind == "artist"
        assert parent.status == "pending"
        assert result["children"] == 2

        children = db.query(Request).filter(Request.parent_request_id == parent.id).all()
        assert {c.release_group_mbid for c in children} == {"rg-a", "rg-b"}
        assert svc.rollup(db, parent) == {"requested": 2, "pending": 2}

    async def test_approving_a_discography_pushes_all_mbids_in_one_call(
            self, db, owner, monkeypatch, lidarr):
        """One push for the whole discography, so monitoring collapses once."""
        async def fake_plan(db_, artist_mbid, *, scope="studio", mb=None):
            return {
                "artist_mbid": "artist-1", "artist_name": "Daft Punk",
                "disambiguation": None, "scope": scope,
                "albums": [
                    {"release_group_mbid": "rg-a", "title": "A", "year": 2001,
                     "primary_type": "Album"},
                    {"release_group_mbid": "rg-b", "title": "B", "year": 2005,
                     "primary_type": "Album"},
                ],
                "summary": {"requestable": 2, "owned": 0, "already_requested": 0,
                            "total": 2},
            }

        monkeypatch.setattr(svc, "plan_artist_request", fake_plan)
        created = await svc.create_artist_request(db, as_dict(owner), "artist-1")

        await svc.approve(db, created["request"], as_dict(owner))

        lidarr.add_albums_by_mbid.assert_awaited_once()
        pushed = lidarr.add_albums_by_mbid.await_args.args[0]
        assert sorted(pushed) == ["rg-a", "rg-b"]

    async def test_nothing_to_request_is_refused(self, db, owner, monkeypatch):
        async def fake_plan(db_, artist_mbid, *, scope="studio", mb=None):
            return {"artist_mbid": "artist-1", "artist_name": "X",
                    "disambiguation": None, "scope": scope, "albums": [],
                    "summary": {"requestable": 0, "owned": 3,
                                "already_requested": 0, "total": 3}}

        monkeypatch.setattr(svc, "plan_artist_request", fake_plan)

        with pytest.raises(svc.RequestError) as exc:
            await svc.create_artist_request(db, as_dict(owner), "artist-1")
        assert exc.value.code == "nothing_to_request"
