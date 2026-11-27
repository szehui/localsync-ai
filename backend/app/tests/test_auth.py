"""Tests for the authentication and sync endpoints (JWT-based)."""
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import LoginRequest


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests to prevent state leakage."""
    import app.routers.auth as auth_module

    auth_module._current_client = None
    auth_module._current_sync_service = None
    yield


@pytest.fixture
def mock_navidrome_client():
    """Create and inject a mock NavidromeClient that responds to ping."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}
    mock_client.get_artists = AsyncMock(return_value=[])
    mock_client.get_album_list2 = AsyncMock(return_value=[])
    return mock_client


@pytest.fixture
def mock_db():
    """Mock DB session for endpoints that need it.
    Also patches SessionLocal in the auth module to return this mock.

    Login now inserts a tenant row, and a MagicMock cannot produce an
    autoincrement id — the JWT would carry uid=None and every protected
    endpoint would then 401. So ``add`` hands User rows an id, which is all the
    mocked flow needs; the multi-user tests below use a real session instead.
    """
    from app.models.database import User

    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    next_id = {"value": 0}

    def _add(obj):
        if isinstance(obj, User) and obj.id is None:
            next_id["value"] += 1
            obj.id = next_id["value"]

    mock.add.side_effect = _add
    with patch('app.routers.auth.SessionLocal') as mock_session_local:
        mock_session_local.return_value = mock
        yield mock


def _login(client, monkeypatch, mock_navidrome_client, mock_db):
    """Helper: login and return the JWT access token.

    This establishes a full connection (client + sync_service are created).
    """
    import app.routers.auth as auth_module
    from app.models.database import get_db

    monkeypatch.setattr(
        auth_module, "NavidromeClient", lambda *args, **kwargs: mock_navidrome_client
    )
    # Override the get_db dependency for the endpoint
    app.dependency_overrides[get_db] = lambda: mock_db

    creds = LoginRequest(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    assert response.status_code == 200, f"Login failed: {response.text}"
    data = response.json()
    assert "access_token" in data
    return data["access_token"]


def _token_with_disconnected_state(client, monkeypatch, mock_navidrome_client, mock_db):
    """Helper: get a valid JWT token but with globals cleared (disconnected state)."""
    import app.routers.auth as auth_module

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    # Clear the connection state so it acts like a disconnected user
    auth_module._current_client = None
    auth_module._current_sync_service = None
    return token


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


# ─── Unauthenticated access ────────────────────────────────────────────────


def test_protected_routes_reject_without_token(client):
    """All protected endpoints should return 401 when no token is provided."""
    protected = [
        ("GET", "/api/auth/status"),
        ("GET", "/api/auth/sync-status"),
        ("POST", "/api/auth/sync"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/me"),
    ]
    for method, path in protected:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} should be 401"


# ─── Login ─────────────────────────────────────────────────────────────────


def test_login_success(client, monkeypatch, mock_navidrome_client, mock_db):
    """Successful login should return a JWT token."""
    import app.routers.auth as auth_module
    from app.models.database import get_db

    monkeypatch.setattr(
        auth_module, "NavidromeClient", lambda *args, **kwargs: mock_navidrome_client
    )
    app.dependency_overrides[get_db] = lambda: mock_db

    creds = LoginRequest(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Should have created client and sync service
    assert auth_module._current_client is not None
    assert auth_module._current_sync_service is not None

    app.dependency_overrides.pop(get_db, None)


def test_login_failure(client, monkeypatch):
    """Failed login should return 401 with error message."""
    mock_client = AsyncMock()
    mock_client.ping.side_effect = Exception("Connection refused")

    import app.routers.auth as auth_module
    from app.models.database import get_db

    mock_db = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    monkeypatch.setattr(
        auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client
    )

    creds = LoginRequest(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    assert response.status_code == 401
    assert "Connection refused" in response.json()["detail"]

    app.dependency_overrides.pop(get_db, None)


# ─── /me ────────────────────────────────────────────────────────────────────


def test_me_returns_user_info(client, monkeypatch, mock_navidrome_client, mock_db):
    """GET /auth/me should return username and navidrome url from the token."""
    import app.routers.auth as auth_module

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    auth_module._current_client = mock_navidrome_client  # keep connected

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "user"
    assert data["navidrome_url"] == "http://test"
    assert data["server_version"] == "0.50.0"


def test_me_disconnected_still_works(client, monkeypatch, mock_navidrome_client, mock_db):
    """GET /auth/me should work even when not connected to Navidrome."""
    token = _token_with_disconnected_state(client, monkeypatch, mock_navidrome_client, mock_db)

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "user"
    assert data["navidrome_url"] == "http://test"
    assert data["server_version"] is None  # no client to ping


# ─── Status ────────────────────────────────────────────────────────────────


def test_connection_status_disconnected(client, monkeypatch, mock_navidrome_client, mock_db):
    """When not connected, status should report disconnected."""
    token = _token_with_disconnected_state(client, monkeypatch, mock_navidrome_client, mock_db)

    response = client.get("/api/auth/status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is False
    assert data["message"] == "Not configured"


def test_connection_status_connected(client, monkeypatch, mock_navidrome_client, mock_db):
    """When connected, status should report connected with server version."""
    import app.routers.auth as auth_module

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    auth_module._current_client = mock_navidrome_client  # keep connected

    response = client.get("/api/auth/status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is True
    assert "v0.50.0" in data["message"]


# ─── Sync ──────────────────────────────────────────────────────────────────


def test_sync_status_not_connected(client, monkeypatch, mock_navidrome_client, mock_db):
    """Sync status when not connected should report 'Not connected'."""
    token = _token_with_disconnected_state(client, monkeypatch, mock_navidrome_client, mock_db)

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Not connected"
    assert data["track_count"] == 0
    assert data["album_count"] == 0
    assert data["artist_count"] == 0


def test_sync_status_idle_when_connected(client, monkeypatch, mock_navidrome_client, mock_db):
    """Sync status when connected should show 'Idle'."""
    import app.routers.auth as auth_module

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    # sync_service exists but never synced -> last_stats is None -> message is 'Idle'
    assert auth_module._current_sync_service is not None
    auth_module._current_sync_service.last_stats = None

    response = client.get("/api/auth/sync-status", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Idle"


def test_sync_not_connected(client, monkeypatch, mock_navidrome_client, mock_db):
    """Sync endpoint should return 400 when not connected."""
    token = _token_with_disconnected_state(client, monkeypatch, mock_navidrome_client, mock_db)

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 400
    assert "Not connected to Navidrome" in response.json()["detail"]


def test_sync_already_in_progress(client, monkeypatch, mock_navidrome_client, mock_db):
    """Sync should return 409 if a sync is already running."""
    import app.routers.auth as auth_module

    mock_sync_service = MagicMock()
    mock_sync_service.is_syncing = True
    mock_sync_service.last_sync = None

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    auth_module._current_client = mock_navidrome_client
    auth_module._current_sync_service = mock_sync_service

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 409
    assert "Sync already in progress" in response.json()["detail"]


def test_sync_success(client, monkeypatch, mock_navidrome_client, mock_db):
    """Successful sync should return 'Sync started'."""
    import app.routers.auth as auth_module
    from app.models.database import get_db

    mock_sync_service = MagicMock()
    mock_sync_service.is_syncing = False
    mock_sync_service.last_sync = None
    mock_sync_service.full_sync = AsyncMock(
        return_value={"tracks": 10, "albums": 2, "artists": 1}
    )

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    auth_module._current_client = mock_navidrome_client
    auth_module._current_sync_service = mock_sync_service

    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post("/api/auth/sync", headers=_auth_header(token))
    assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
    data = response.json()
    assert data["is_syncing"] is True
    assert data["message"] == "Sync started"

    app.dependency_overrides.pop(get_db, None)


# ─── Logout ────────────────────────────────────────────────────────────────


def test_logout_clears_client(client, monkeypatch, mock_navidrome_client, mock_db):
    """Logout should clear the current client connection."""
    import app.routers.auth as auth_module

    token = _login(client, monkeypatch, mock_navidrome_client, mock_db)
    auth_module._current_client = mock_navidrome_client
    assert auth_module._current_client is not None

    response = client.post("/api/auth/logout", headers=_auth_header(token))
    assert response.status_code == 200
    assert auth_module._current_client is None


# ─── Token validation ──────────────────────────────────────────────────────


def test_invalid_token_rejected(client):
    """A garbage token should be rejected with 401."""
    response = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer invalidtoken"}
    )
    assert response.status_code == 401


def test_expired_token_rejected(client):
    """A token with a past expiration should be rejected."""
    import jwt
    from app.config import settings

    expired = jwt.encode(
        {"sub": "user", "url": "http://test", "exp": 0},
        settings.jwt_secret_key,  # Fixed: was .get_secret_value() but it's a plain str
        algorithm=settings.jwt_algorithm,
    )
    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401


# ─── Multi-user: tenant id in the token ────────────────────────────────────


@pytest.fixture
def db():
    """Real in-memory SQLite session for the multi-user tests.

    The ``mock_db`` MagicMock cannot represent User rows and login now inserts
    one, so these tests run against the actual models. StaticPool keeps every
    thread on one connection — TestClient dispatches the request from its own
    thread — so the app sees the same in-memory database as the test.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.database import Base, get_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


def _login_as(client, monkeypatch, mock_navidrome_client, db, username="user"):
    """Log in against a real session and return the JWT."""
    import app.routers.auth as auth_module
    from app.models.database import get_db

    monkeypatch.setattr(
        auth_module, "NavidromeClient", lambda *args, **kwargs: mock_navidrome_client
    )
    app.dependency_overrides[get_db] = lambda: db

    creds = LoginRequest(url="http://test", username=username, password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    assert response.status_code == 200, f"Login failed: {response.text}"
    return response.json()["access_token"]


def _decode(token):
    from app.config import settings

    return jwt.decode(
        token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )


def test_login_token_carries_uid(client, monkeypatch, mock_navidrome_client, db):
    """A freshly minted token must name the tenant it belongs to."""
    from app.models.database import User

    token = _login_as(client, monkeypatch, mock_navidrome_client, db)
    payload = _decode(token)
    assert "uid" in payload
    assert isinstance(payload["uid"], int)
    assert payload["uid"] == db.query(User).one().id


def test_get_current_user_returns_id(client, monkeypatch, mock_navidrome_client, db):
    """The dependency hands callers the tenant id; /me still echoes the login."""
    import asyncio

    from app.routers.auth import get_current_user

    token = _login_as(client, monkeypatch, mock_navidrome_client, db)
    uid = _decode(token)["uid"]

    user = asyncio.run(get_current_user(authorization=f"Bearer {token}"))
    assert user["id"] == uid
    assert user["username"] == "user"

    response = client.get("/api/auth/me", headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json()["username"] == "user"


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


def test_second_login_creates_second_user(
    client, monkeypatch, mock_navidrome_client, db
):
    """Each distinct login is its own tenant; nobody shares a row."""
    from app.models.database import User

    _login_as(client, monkeypatch, mock_navidrome_client, db, username="user")
    _login_as(client, monkeypatch, mock_navidrome_client, db, username="other")

    users = db.query(User).all()
    assert len(users) == 2
    assert {u.username for u in users} == {"user", "other"}