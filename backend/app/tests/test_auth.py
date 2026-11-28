"""Tests for the authentication, connection, sync, and scrobbler endpoints.

Every endpoint here acts on the calling user, so these tests run against a real
in-memory SQLite database (a MagicMock cannot represent User rows, let alone
per-user scoping) and real registry entries in app.services.users.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, User, get_db
from app.models.schemas import LoginRequest


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear the per-user client registry between tests.

    The registry is process-wide state keyed by user id, so leaking it would
    make an unrelated test look connected (or, worse, hide a real disconnect).
    """
    import app.services.users as users_service

    users_service._clients.clear()
    users_service._sync_services.clear()
    yield
    users_service._clients.clear()
    users_service._sync_services.clear()
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db():
    """Real in-memory SQLite session, also installed as the get_db override.

    StaticPool keeps every thread on one connection — TestClient dispatches the
    request from its own thread — so the app sees the same in-memory database as
    the test.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Mirror production: SQLite only honors ON DELETE CASCADE with this pragma.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


def _patch_navidrome(monkeypatch, factory):
    """Patch the client class in both namespaces that construct it.

    app.routers.auth builds the client on login, app.services.users builds one
    when it lazily reconnects; patching only one would send the other to the
    real network.
    """
    import app.routers.auth as auth_module
    import app.services.users as users_service

    monkeypatch.setattr(auth_module, "NavidromeClient", factory)
    monkeypatch.setattr(users_service, "NavidromeClient", factory)


def _login_as(
    client,
    monkeypatch,
    db,
    username="user",
    url="http://test",
    version="0.50.0",
    expect=200,
):
    """Log in for real and return (token, navidrome mock).

    The mock is built per call so two logins in one test get distinct clients —
    which is what makes the per-caller assertions meaningful.

    Pass ``expect`` to assert a refusal instead; the raw response is returned
    then, since there is no token to hand back.
    """
    nav = AsyncMock()
    nav.ping.return_value = {"version": version}
    _patch_navidrome(monkeypatch, lambda *args, **kwargs: nav)

    creds = LoginRequest(url=url, username=username, password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    if expect != 200:
        assert response.status_code == expect, f"Expected {expect}: {response.text}"
        return response
    assert response.status_code == 200, f"Login failed: {response.text}"
    return response.json()["access_token"], nav


def _uid(db, username):
    return db.query(User).filter(User.username == username).one().id


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def _token_for(uid, username="ghost", url="http://test"):
    """Mint a valid token for a tenant.

    Note this only signs the claims: the API now also checks the tenant row
    still exists, so a token minted here authenticates only if a matching row
    is in the DB (see _ghost_tenant).
    """
    from app.config import settings

    return jwt.encode(
        {"sub": username, "url": url, "uid": uid, "exp": 9999999999},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _ghost_tenant(db, uid=999, username="ghost", url="http://test"):
    """Create the tenant row a bare token needs to authenticate.

    Tests that only care about "a user with no stored credentials" mint a token
    rather than logging in. That token is now checked against the users table,
    so the row has to exist — an id with no row is exactly the deleted-account
    case, which must 401.
    """
    if db.get(User, uid) is None:
        db.add(User(id=uid, username=username, navidrome_url=url,
                    navidrome_password="secret", is_owner=False))
        db.commit()
    return uid


def _decode(token):
    from app.config import settings

    return jwt.decode(
        token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )


# ─── Unauthenticated access ────────────────────────────────────────────────


def test_protected_routes_reject_without_token(client):
    """All protected endpoints should return 401 when no token is provided."""
    protected = [
        ("GET", "/api/auth/status"),
        ("GET", "/api/auth/sync-status"),
        ("POST", "/api/auth/sync"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/auth/lastfm"),
        ("GET", "/api/auth/listenbrainz"),
        ("POST", "/api/auth/connect"),
        ("POST", "/api/auth/disconnect"),
    ]
    for method, path in protected:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} should be 401"


# ─── Login ─────────────────────────────────────────────────────────────────


def test_login_success(client, monkeypatch, db):
    """Successful login returns a JWT and registers the caller's client."""
    import app.services.users as users_service

    token, nav = _login_as(client, monkeypatch, db)

    payload = _decode(token)
    assert payload["sub"] == "user"
    assert payload["url"] == "http://test"
    assert payload["uid"] == _uid(db, "user")

    # The live client and its sync service belong to the tenant, not the process.
    assert users_service.peek_client(payload["uid"]) is nav
    assert users_service.peek_sync_service(payload["uid"]) is not None
    assert users_service.peek_sync_service(payload["uid"]).user_id == payload["uid"]


def test_login_stores_credentials_for_scheduled_jobs(client, monkeypatch, db):
    """Login refreshes the stored password the scheduler replays later."""
    token, _nav = _login_as(client, monkeypatch, db)
    user = db.get(User, _decode(token)["uid"])
    assert user.navidrome_url == "http://test"
    assert user.navidrome_password == "pass"


def test_login_failure(client, monkeypatch, db):
    """Failed login should return 401 with the error message and no user row."""
    import app.routers.auth as auth_module

    mock_client = AsyncMock()
    mock_client.ping.side_effect = Exception("Connection refused")
    _patch_navidrome(monkeypatch, lambda *args, **kwargs: mock_client)

    creds = LoginRequest(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())

    assert response.status_code == 401
    assert "Connection refused" in response.json()["detail"]
    # A rejected login must not leave a client or a tenant behind.
    assert db.query(User).count() == 0
    mock_client.close.assert_awaited()


def test_second_login_creates_second_user(client, monkeypatch, db):
    """Each distinct login is its own tenant; nobody shares a row."""
    _login_as(client, monkeypatch, db, username="user")
    _login_as(client, monkeypatch, db, username="other")

    users = db.query(User).all()
    assert len(users) == 2
    assert {u.username for u in users} == {"user", "other"}


def test_same_username_other_url_is_refused_not_duplicated(client, monkeypatch, db):
    """A second URL for an existing username is refused, not minted as a tenant.

    This reverses the original multi-user contract, on evidence: the same
    Navidrome server is commonly reached at two addresses (public hostname and
    LAN IP), and logging in at the second one silently created an empty tenant
    with no play history, no discovery seeds and no playlists. Refusing the
    login and naming the existing URL is recoverable; a silent second account
    is not. Genuinely separate servers still work, provided the usernames
    differ — see test_different_username_on_other_url_is_created.
    """
    _login_as(client, monkeypatch, db, username="user", url="http://a")
    response = _login_as(client, monkeypatch, db, username="user", url="http://b",
                         expect=409)

    assert response.status_code == 409
    assert "http://a" in response.json()["detail"]
    assert db.query(User).count() == 1


def test_different_username_on_other_url_is_created(client, monkeypatch, db):
    """The other-server case still works when the usernames actually differ."""
    _login_as(client, monkeypatch, db, username="user", url="http://a")
    _login_as(client, monkeypatch, db, username="other", url="http://b")

    assert db.query(User).count() == 2


def test_relogin_replaces_only_that_users_client(client, monkeypatch, db):
    """A second login for the same tenant closes the superseded client."""
    import app.services.users as users_service

    token, first = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]

    _token2, second = _login_as(client, monkeypatch, db)
    assert second is not first
    assert users_service.peek_client(uid) is second
    first.close.assert_awaited()


# ─── Token validation ──────────────────────────────────────────────────────


def test_invalid_token_rejected(client):
    """A garbage token should be rejected with 401."""
    response = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer invalidtoken"}
    )
    assert response.status_code == 401


def test_expired_token_rejected(client):
    """A token with a past expiration should be rejected."""
    from app.config import settings

    expired = jwt.encode(
        {"sub": "user", "url": "http://test", "uid": 1, "exp": 0},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401


def test_token_without_uid_is_rejected(client):
    """A pre-multi-user token has no tenant — 401, not a guess at user 1."""
    from app.config import settings

    legacy = jwt.encode(
        {"sub": "user", "url": "http://test", "exp": 9999999999},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get("/api/auth/me", headers=_auth_header(legacy))
    assert response.status_code == 401
    assert "log in again" in response.json()["detail"]


def test_get_current_user_returns_id(client, monkeypatch, db):
    """The dependency hands callers the tenant id; /me still echoes the login."""
    import asyncio

    from app.routers.auth import get_current_user

    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]

    user = asyncio.run(get_current_user(authorization=f"Bearer {token}", db=db))
    assert user["id"] == uid
    assert user["username"] == "user"
    assert user["navidrome_url"] == "http://test"


# ─── /me ────────────────────────────────────────────────────────────────────


def test_me_returns_user_info(client, monkeypatch, db):
    """GET /auth/me returns the token's identity and the live server version."""
    token, _nav = _login_as(client, monkeypatch, db)

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "user"
    assert data["navidrome_url"] == "http://test"
    assert data["server_version"] == "0.50.0"


def test_me_disconnected_still_works(client, monkeypatch, db):
    """GET /auth/me answers even with no live client, reporting no version."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    users_service._clients.clear()
    users_service._sync_services.clear()

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "user"
    assert data["navidrome_url"] == "http://test"
    assert data["server_version"] is None  # nothing cached to ping


def test_me_does_not_reconnect(client, monkeypatch, db):
    """/me reads the registry only — no client construction on a cache miss."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    users_service._clients.clear()

    calls = []

    def factory(*args, **kwargs):
        calls.append(1)
        raise AssertionError("/me must not build a client")

    _patch_navidrome(monkeypatch, factory)
    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    assert calls == []


# ─── Status ────────────────────────────────────────────────────────────────


def test_connection_status_not_configured(client, monkeypatch, db):
    """A token for a tenant with no stored credentials reports 'Not configured'."""
    nav = AsyncMock()
    _patch_navidrome(monkeypatch, lambda *args, **kwargs: nav)

    response = client.get("/api/auth/status", headers=_auth_header(_token_for(_ghost_tenant(db))))
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is False
    assert data["message"] == "Not configured"


def test_connection_status_connected(client, monkeypatch, db):
    """When connected, status reports connected with the server version."""
    token, _nav = _login_as(client, monkeypatch, db)

    response = client.get("/api/auth/status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is True
    assert "v0.50.0" in data["message"]
    assert data["server_version"] == "0.50.0"


def test_connection_status_reconnects_lazily(client, monkeypatch, db):
    """A cleared registry is rebuilt from the stored credentials, not failed."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]
    users_service._clients.clear()

    response = client.get("/api/auth/status", headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert users_service.peek_client(uid) is not None


def test_connection_status_unreachable_server(client, monkeypatch, db):
    """A server that stopped answering reports 'Not configured'."""
    token, nav = _login_as(client, monkeypatch, db)
    nav.ping.side_effect = Exception("connection reset")

    response = client.get("/api/auth/status", headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "message": "Not configured",
        "server_version": None,
    }


# ─── Sync status ───────────────────────────────────────────────────────────


def test_sync_status_not_connected(client, monkeypatch, db):
    """Sync status with no service for this tenant reports 'Not connected'."""
    token = _token_for(_ghost_tenant(db))

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Not connected"
    assert data["track_count"] == 0
    assert data["album_count"] == 0
    assert data["artist_count"] == 0


def test_sync_status_idle_when_connected(client, monkeypatch, db):
    """A service that never synced reports 'Idle'."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    svc = users_service.peek_sync_service(_decode(token)["uid"])
    assert svc is not None
    svc.last_stats = None

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Idle"


def test_sync_status_reports_counts(client, monkeypatch, db):
    """After a sync the counts and the summary sentence are reported."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    svc = users_service.peek_sync_service(_decode(token)["uid"])
    svc.last_stats = {"tracks": 10, "albums": 2, "artists": 1}
    svc.last_sync = datetime(2026, 1, 2, 3, 4, 5)

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    data = response.json()
    assert data["track_count"] == 10
    assert data["album_count"] == 2
    assert data["artist_count"] == 1
    assert data["message"] == "Idle — last sync: 10 tracks, 2 albums, 1 artists"


def test_sync_status_reports_syncing(client, monkeypatch, db):
    """A running sync is reported as 'Syncing...'."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    users_service.peek_sync_service(_decode(token)["uid"]).is_syncing = True

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    data = response.json()
    assert data["is_syncing"] is True
    assert data["message"] == "Syncing..."


def test_status_and_sync_status_are_per_caller(client, monkeypatch, db):
    """Two tenants see their own client and their own sync service."""
    import app.services.users as users_service

    token_a, _nav_a = _login_as(client, monkeypatch, db, username="user", version="0.50.0")
    token_b, _nav_b = _login_as(client, monkeypatch, db, username="other", version="0.51.0")
    uid_a = _decode(token_a)["uid"]
    uid_b = _decode(token_b)["uid"]
    assert uid_a != uid_b

    users_service.peek_sync_service(uid_a).last_stats = {
        "tracks": 10, "albums": 2, "artists": 1,
    }
    users_service.peek_sync_service(uid_b).is_syncing = True

    status_a = client.get("/api/auth/status", headers=_auth_header(token_a)).json()
    status_b = client.get("/api/auth/status", headers=_auth_header(token_b)).json()
    assert status_a["server_version"] == "0.50.0"
    assert status_b["server_version"] == "0.51.0"

    sync_a = client.get("/api/auth/sync-status", headers=_auth_header(token_a)).json()
    sync_b = client.get("/api/auth/sync-status", headers=_auth_header(token_b)).json()
    assert sync_a["message"] == "Idle — last sync: 10 tracks, 2 albums, 1 artists"
    assert sync_b["message"] == "Syncing..."
    # A's state must not leak into B's counts.
    assert sync_b["track_count"] == 0


# ─── Trigger sync ──────────────────────────────────────────────────────────


def test_sync_not_connected(client, monkeypatch, db):
    """Sync endpoint returns 400 when the tenant has no usable client.

    This used to pass a token for a user id with no row, and got its 400 from
    the missing row. That case is now a 401 (a deleted account), so the error
    path is exercised where it actually lives: client construction failing.
    """
    token, _nav = _login_as(client, monkeypatch, db)

    async def _no_client(*args, **kwargs):
        raise RuntimeError("no stored credentials")

    import app.routers.auth as auth_module

    monkeypatch.setattr(auth_module, "get_client", _no_client)

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 400
    assert "Not connected to Navidrome" in response.json()["detail"]


def test_sync_already_in_progress(client, monkeypatch, db):
    """Sync returns 409 if this user's sync is already running."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    users_service.peek_sync_service(_decode(token)["uid"]).is_syncing = True

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 409
    assert "Sync already in progress" in response.json()["detail"]


def test_sync_success(client, monkeypatch, db):
    """Triggering a sync starts it for this tenant and returns right away."""
    import app.routers.auth as auth_module
    import app.services.users as users_service

    token, nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]

    fake_svc = MagicMock()
    fake_svc.is_syncing = False
    fake_svc.last_sync = None
    fake_svc.last_stats = None
    fake_svc.user_id = uid
    fake_svc.full_sync = AsyncMock(return_value={"tracks": 1, "albums": 0, "artists": 0})
    monkeypatch.setattr(auth_module, "SyncService", lambda client, user_id: fake_svc)

    # The background task opens its own session; keep it off the real database.
    monkeypatch.setattr(auth_module, "SessionLocal", lambda: MagicMock())

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
    data = response.json()
    assert data["is_syncing"] is True
    assert data["message"] == "Sync started"

    # The new service was published for this tenant, alongside the same client.
    assert users_service.peek_sync_service(uid) is fake_svc
    assert users_service.peek_client(uid) is nav


def test_sync_is_per_caller(client, monkeypatch, db):
    """A's running sync does not block B's trigger."""
    import app.routers.auth as auth_module
    import app.services.users as users_service

    token_a, _nav_a = _login_as(client, monkeypatch, db, username="user")
    token_b, _nav_b = _login_as(client, monkeypatch, db, username="other")

    users_service.peek_sync_service(_decode(token_a)["uid"]).is_syncing = True

    fake_svc = MagicMock()
    fake_svc.is_syncing = False
    fake_svc.last_sync = None
    fake_svc.last_stats = None
    fake_svc.full_sync = AsyncMock(return_value={})
    monkeypatch.setattr(auth_module, "SyncService", lambda client, user_id: fake_svc)
    monkeypatch.setattr(auth_module, "SessionLocal", lambda: MagicMock())

    assert client.post("/api/auth/sync", headers=_auth_header(token_a)).status_code == 409
    assert client.post("/api/auth/sync", headers=_auth_header(token_b)).status_code == 200


# ─── Logout ────────────────────────────────────────────────────────────────


def test_logout_clears_client(client, monkeypatch, db):
    """Logout drops the caller's client and sync service."""
    import app.services.users as users_service

    token, nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]
    assert users_service.peek_client(uid) is not None

    response = client.post("/api/auth/logout", headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "message": "Logged out",
        "server_version": None,
    }
    assert users_service.peek_client(uid) is None
    assert users_service.peek_sync_service(uid) is None
    nav.close.assert_awaited()


def test_logout_leaves_other_users_connected(client, monkeypatch, db):
    """THE regression: logging one user out must not disconnect everyone."""
    import app.services.users as users_service

    token_a, nav_a = _login_as(client, monkeypatch, db, username="user")
    token_b, nav_b = _login_as(client, monkeypatch, db, username="other")
    uid_a = _decode(token_a)["uid"]
    uid_b = _decode(token_b)["uid"]

    assert client.post("/api/auth/logout", headers=_auth_header(token_b)).status_code == 200

    assert users_service.peek_client(uid_a) is nav_a
    assert users_service.peek_client(uid_a) is not None
    assert users_service.peek_sync_service(uid_a) is not None
    assert users_service.peek_client(uid_b) is None
    nav_b.close.assert_awaited()
    nav_a.close.assert_not_awaited()

    # A is still fully usable after B logged out.
    status = client.get("/api/auth/status", headers=_auth_header(token_a)).json()
    assert status["connected"] is True


def test_logout_does_not_delete_credentials(client, monkeypatch, db):
    """Scheduled jobs replay stored credentials, so logout keeps the row."""
    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]

    client.post("/api/auth/logout", headers=_auth_header(token))

    user = db.get(User, uid)
    assert user is not None
    assert user.navidrome_password == "pass"


# ─── Legacy /connect + /disconnect ─────────────────────────────────────────


def test_legacy_connect_establishes_state(client, monkeypatch, db):
    """The legacy alias still connects and reports the version."""
    import app.services.users as users_service

    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]
    users_service._clients.clear()
    users_service._sync_services.clear()

    nav = AsyncMock()
    nav.ping.return_value = {"version": "0.52.0"}
    _patch_navidrome(monkeypatch, lambda *args, **kwargs: nav)

    response = client.post(
        "/api/auth/connect",
        headers=_auth_header(token),
        json=LoginRequest(url="http://test", username="user", password="pass").model_dump(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["server_version"] == "0.52.0"
    assert users_service.peek_client(uid) is nav


def test_legacy_connect_failure_returns_400(client, monkeypatch, db):
    """A failed legacy connect is a 400, not a 401."""
    token, _nav = _login_as(client, monkeypatch, db)

    nav = AsyncMock()
    nav.ping.side_effect = Exception("Connection refused")
    _patch_navidrome(monkeypatch, lambda *args, **kwargs: nav)

    response = client.post(
        "/api/auth/connect",
        headers=_auth_header(token),
        json=LoginRequest(url="http://test", username="user", password="pass").model_dump(),
    )
    assert response.status_code == 400
    assert "Connection refused" in response.json()["detail"]


def test_legacy_disconnect_only_clears_caller(client, monkeypatch, db):
    """The legacy disconnect alias is caller-scoped and keeps credentials."""
    import app.services.users as users_service

    token_a, _nav_a = _login_as(client, monkeypatch, db, username="user")
    token_b, _nav_b = _login_as(client, monkeypatch, db, username="other")
    uid_a = _decode(token_a)["uid"]
    uid_b = _decode(token_b)["uid"]

    response = client.post("/api/auth/disconnect", headers=_auth_header(token_b))
    assert response.status_code == 200
    assert response.json()["message"] == "Disconnected"
    assert users_service.peek_client(uid_a) is not None
    assert users_service.peek_client(uid_b) is None
    assert db.get(User, uid_b) is not None


# ─── Per-user scrobbler config ─────────────────────────────────────────────


def test_lastfm_config_is_per_user(client, monkeypatch, db):
    """One user's Last.fm key is invisible to another and is not global."""
    token_a, _nav_a = _login_as(client, monkeypatch, db, username="user")
    token_b, _nav_b = _login_as(client, monkeypatch, db, username="other")

    put = client.put(
        "/api/auth/lastfm",
        headers=_auth_header(token_a),
        json={"api_key": "key-a", "username": "lf-a"},
    )
    assert put.status_code == 200
    assert put.json() == {"configured": True, "username": "lf-a"}

    assert client.get("/api/auth/lastfm", headers=_auth_header(token_a)).json() == {
        "configured": True, "username": "lf-a",
    }
    assert client.get("/api/auth/lastfm", headers=_auth_header(token_b)).json() == {
        "configured": False, "username": None,
    }

    # DELETE only removes the caller's row.
    assert client.delete("/api/auth/lastfm", headers=_auth_header(token_b)).status_code == 200
    assert client.get("/api/auth/lastfm", headers=_auth_header(token_a)).json()["configured"] is True
    assert client.delete("/api/auth/lastfm", headers=_auth_header(token_a)).status_code == 200
    assert client.get("/api/auth/lastfm", headers=_auth_header(token_a)).json()["configured"] is False


def test_listenbrainz_config_is_per_user(client, monkeypatch, db):
    """ListenBrainz credentials are stored per tenant and never echoed back."""
    token_a, _nav_a = _login_as(client, monkeypatch, db, username="user")
    token_b, _nav_b = _login_as(client, monkeypatch, db, username="other")

    put = client.put(
        "/api/auth/listenbrainz",
        headers=_auth_header(token_a),
        json={"username": "lb-a", "token": "super-secret-token"},
    )
    assert put.status_code == 200
    assert "super-secret-token" not in put.text

    body = client.get("/api/auth/listenbrainz", headers=_auth_header(token_a)).json()
    assert body == {"configured": True, "username": "lb-a"}
    assert client.get("/api/auth/listenbrainz", headers=_auth_header(token_b)).json() == {
        "configured": False, "username": None,
    }

    assert client.delete("/api/auth/listenbrainz", headers=_auth_header(token_a)).status_code == 200
    assert client.get("/api/auth/listenbrainz", headers=_auth_header(token_a)).json()["configured"] is False


# ─── Deleted accounts ───────────────────────────────────────────────────────


def test_token_for_deleted_user_is_rejected(client, monkeypatch, db):
    """A valid token must stop working when its tenant row is gone.

    Found live: deleting the probe account from the Phase 5 verification left
    its token authenticating — /api/auth/me and /api/library/stats answered 200
    for a user that no longer existed, and endpoints joining on the user row
    500'd. Signature validity is not authorisation; the row has to exist.
    """
    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]
    assert client.get("/api/auth/me", headers=_auth_header(token)).status_code == 200

    db.query(User).filter(User.id == uid).delete()
    db.commit()

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 401
    assert "no longer exists" in response.json()["detail"]


def test_token_for_never_existing_user_is_rejected(client, monkeypatch, db):
    """A signed token for an id that was never issued does not authenticate."""
    response = client.get("/api/auth/me", headers=_auth_header(_token_for(4242)))
    assert response.status_code == 401
    assert "no longer exists" in response.json()["detail"]


def test_deleted_user_token_cannot_read_other_endpoints(client, monkeypatch, db):
    """The check is in the shared dependency, so every router is covered."""
    token, _nav = _login_as(client, monkeypatch, db)
    uid = _decode(token)["uid"]
    db.query(User).filter(User.id == uid).delete()
    db.commit()

    for path in ("/api/auth/sync-status", "/api/auth/listenbrainz"):
        assert client.get(path, headers=_auth_header(token)).status_code == 401, path
