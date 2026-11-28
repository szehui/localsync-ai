"""Requests API: scoping, permissions, and error mapping.

Two conventions this pins, both consistent with the rest of the app:

* another member's request answers **404**, never 403 — a 403 would confirm the
  row exists;
* a duplicate ask returns the *existing* request with the caller's vote, never a
  second row, because a second row means a second Lidarr add.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, Request, User, get_db
from app.routers.auth import get_current_user
from app.services import requests as svc

RG = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

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
def people(db):
    owner = User(username="sze", display_name="Sze", navidrome_url="http://nav",
                 navidrome_password="x", is_owner=True)
    guest = User(username="rebecca", display_name="Rebecca", navidrome_url="http://nav",
                 navidrome_password="x")
    db.add_all([owner, guest])
    db.commit()
    return owner, guest


@pytest.fixture
def client(db, people):
    owner, guest = people
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": owner.id, "username": owner.username,
        "navidrome_url": owner.navidrome_url,
    }
    # No context manager: entering it runs the app lifespan, which starts the
    # APScheduler instance and raises SchedulerAlreadyRunningError on the second
    # test. Requests still work; startup side effects are not under test here.
    yield TestClient(app)
    app.dependency_overrides.clear()


def as_guest(people):
    owner, guest = people
    app.dependency_overrides[get_current_user] = lambda: {
        "id": guest.id, "username": guest.username,
        "navidrome_url": guest.navidrome_url,
    }


@pytest.fixture
def mb(monkeypatch):
    async def fake_fetch(db, mbid, *, mb=None):
        return {
            "id": mbid, "title": "Random Access Memories",
            "primary-type": "Album", "secondary-types": [],
            "first-release-date": "2013-05-17",
            "artist-credit": [{"name": "Daft Punk",
                               "artist": {"id": "artist-1"}}],
        }

    monkeypatch.setattr(svc, "fetch_release_group", fake_fetch)
    monkeypatch.setattr(svc, "_cover", AsyncMock(return_value=None))


@pytest.fixture
def lidarr(monkeypatch):
    service = MagicMock()
    service.add_albums_by_mbid = AsyncMock(return_value={
        "results": [{"release_group_mbid": RG, "album_id": 10, "artist_id": 1}],
        "added": [], "already_present": [], "unresolved": [], "failed": [],
        "monitoring_ok": True,
    })
    monkeypatch.setattr(svc, "_lidarr_service", lambda db: service)
    return service


# ── Auth ────────────────────────────────────────────────────────────────────


def test_requires_authentication(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides.pop(get_current_user, None)
    assert TestClient(app).get("/api/requests").status_code == 401
    app.dependency_overrides.clear()


def test_creating_requires_authentication(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides.pop(get_current_user, None)
    assert TestClient(app).post(
        "/api/requests", json={"release_group_mbid": RG}).status_code == 401
    app.dependency_overrides.clear()


# ── Creation ────────────────────────────────────────────────────────────────


def test_create_album_request(client, db, mb):
    response = client.post("/api/requests", json={"release_group_mbid": RG})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["album_title"] == "Random Access Memories"
    assert body["requested_by"] == "Sze"
    assert body["vote_count"] == 1


def test_duplicate_ask_returns_the_existing_request(client, db, people, mb):
    first = client.post("/api/requests", json={"release_group_mbid": RG})
    as_guest(people)
    second = client.post("/api/requests", json={"release_group_mbid": RG})

    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["vote_count"] == 2
    assert db.query(Request).count() == 1


def test_missing_mbid_is_a_400(client, db, mb):
    response = client.post("/api/requests", json={"kind": "album"})
    assert response.status_code == 400


def test_blocked_album_maps_to_403(client, db, people, mb):
    from app.models.database import Blocklist

    owner, _ = people
    db.add(Blocklist(scope="release_group", release_group_mbid=RG,
                     created_by=owner.id))
    db.commit()

    response = client.post("/api/requests", json={"release_group_mbid": RG})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "blocked"


def test_user_without_permission_gets_403(client, db, people, mb):
    _, guest = people
    guest.can_request = False
    db.commit()
    as_guest(people)

    response = client.post("/api/requests", json={"release_group_mbid": RG})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cannot_request"


# ── Listing and scoping ─────────────────────────────────────────────────────


def test_mine_only_shows_own_requests(client, db, people, mb):
    client.post("/api/requests", json={"release_group_mbid": RG})
    as_guest(people)
    body = client.get("/api/requests?scope=mine").json()
    assert body["requests"] == []


def test_all_is_owner_only(client, db, people, mb):
    _, guest = people
    db.add(Request(user_id=guest.id, kind="album", release_group_mbid="rg-x",
                   album_title="X", status="pending"))
    db.commit()
    as_guest(people)

    assert client.get("/api/requests?scope=all").status_code == 403


def test_owner_sees_everything(client, db, people, mb):
    _, guest = people
    db.add(Request(user_id=guest.id, kind="album", release_group_mbid="rg-x",
                   album_title="X", status="pending"))
    db.commit()

    body = client.get("/api/requests?scope=all").json()
    assert body["total"] == 1
    assert body["requests"][0]["requested_by"] == "Rebecca"


def test_another_members_request_is_404(client, db, people, mb):
    owner, guest = people
    db.add(Request(user_id=guest.id, kind="album", release_group_mbid="rg-x",
                   album_title="X", status="pending"))

    # Owner-created request; the guest asks for it by id and must not see it.
    owner_req = Request(user_id=owner.id, kind="album",
                        release_group_mbid="rg-y", album_title="Y",
                        status="pending")
    db.add(owner_req)
    db.commit()

    as_guest(people)
    assert client.get(f"/api/requests/{owner_req.id}").status_code == 404


# ── Lifecycle endpoints ─────────────────────────────────────────────────────


def test_approve_by_owner_pushes_and_records_ids(client, db, mb, lidarr):
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()
    response = client.post(f"/api/requests/{created['id']}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["lidarr_album_id"] == 10
    lidarr.add_albums_by_mbid.assert_awaited_once_with([RG])


def test_approve_by_guest_is_403(client, db, people, mb, lidarr):
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()
    as_guest(people)

    response = client.post(f"/api/requests/{created['id']}/approve")
    assert response.status_code == 404  # not theirs, so invisible

    as_guest_requested = client.post("/api/requests",
                                     json={"release_group_mbid": "22222222-2222-2222-2222-222222222222"})
    response = client.post(f"/api/requests/{as_guest_requested.json()['id']}/approve")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_deny_records_reason(client, db, people, mb):
    _, guest = people
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()

    response = client.post(f"/api/requests/{created['id']}/deny?reason=kidz+bop")
    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert "kidz bop" in response.json()["note"]


def test_guest_can_cancel_own_pending_request(client, db, people, mb):
    as_guest(people)
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()

    response = client.post(f"/api/requests/{created['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_vote_endpoint_is_idempotent(client, db, people, mb):
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()
    as_guest(people)

    first = client.post(f"/api/requests/{created['id']}/vote").json()
    second = client.post(f"/api/requests/{created['id']}/vote").json()

    assert first["added"] is True
    assert first["vote_count"] == 2
    assert second["added"] is False
    assert second["vote_count"] == 2


def test_vote_on_closed_request_is_409(client, db, people, mb):
    created = client.post("/api/requests", json={"release_group_mbid": RG}).json()
    client.post(f"/api/requests/{created['id']}/deny?reason=no")

    as_guest(people)
    assert client.post(f"/api/requests/{created['id']}/vote").status_code == 409


def test_unknown_request_is_404(client, db, mb):
    assert client.get("/api/requests/9999").status_code == 404


# ── Artist plan ─────────────────────────────────────────────────────────────


def test_artist_plan_returns_counts(client, db, monkeypatch):
    async def fake_plan(db_, artist_mbid, *, scope="studio", mb=None):
        return {
            "artist_mbid": artist_mbid, "artist_name": "Daft Punk",
            "disambiguation": None, "scope": scope,
            "albums": [{"release_group_mbid": "rg-a", "title": "A", "year": 2001,
                        "primary_type": "Album"}],
            "summary": {"requestable": 1, "owned": 2, "already_requested": 0,
                        "total": 3},
        }

    monkeypatch.setattr(svc, "plan_artist_request", fake_plan)

    body = client.get("/api/requests/plan?artist_mbid=artist-1&scope=studio").json()
    assert body["summary"]["requestable"] == 1
    assert body["summary"]["owned"] == 2


def test_artist_request_creates_children_and_reports_rollup(client, db, monkeypatch):
    async def fake_plan(db_, artist_mbid, *, scope="studio", mb=None):
        return {
            "artist_mbid": artist_mbid, "artist_name": "Daft Punk",
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

    created = client.post("/api/requests",
                          json={"kind": "artist", "artist_mbid": "artist-1"}).json()

    assert created["kind"] == "artist"
    assert created["rollup"]["pending"] == 2

    detail = client.get(f"/api/requests/{created['id']}").json()
    assert len(detail["children"]) == 2
    assert {c["release_group_mbid"] for c in detail["children"]} == {"rg-a", "rg-b"}


# ── Manual reconciliation ───────────────────────────────────────────────────


def test_sync_is_owner_only(client, people):
    as_guest(people)
    assert client.post("/api/requests/sync").status_code == 403


def test_sync_reports_counts(client, monkeypatch):
    from app.services import request_sync

    calls = {}

    async def fake_reconcile(db, **kwargs):
        calls["ran"] = True
        return {"total": 3, "changed": 2, "failed": 1}

    monkeypatch.setattr(request_sync, "reconcile_open_requests", fake_reconcile)

    response = client.post("/api/requests/sync")

    assert response.status_code == 200
    assert response.json() == {"total": 3, "changed": 2, "failed": 1}
    assert calls.get("ran") is True


def test_sync_is_not_shadowed_by_the_id_route(client, monkeypatch):
    """"sync" must never be parsed as a request id."""
    from app.services import request_sync

    monkeypatch.setattr(
        request_sync, "reconcile_open_requests",
        AsyncMock(return_value={"total": 0, "changed": 0, "failed": 0}))

    response = client.post("/api/requests/sync")

    assert response.status_code == 200
    assert response.json()["total"] == 0
