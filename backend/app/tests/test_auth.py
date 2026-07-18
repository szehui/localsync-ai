"""Tests for the authentication and sync endpoints."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import NavidromeConfig


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


def test_connection_status_unconfigured(client):
    """When no connection is configured, status should be 'Not configured'."""
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is False
    assert data["message"] == "Not configured"
    assert data["server_version"] is None


def test_sync_status_not_connected(client):
    """Sync status when not connected should report 'Not connected'."""
    response = client.get("/api/auth/sync-status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Not connected"
    assert data["track_count"] == 0
    assert data["album_count"] == 0
    assert data["artist_count"] == 0


def test_sync_not_connected(client, reset_globals):
    """Sync endpoint should return 400 when not connected."""
    response = client.post("/api/auth/sync")
    assert response.status_code == 400
    assert "Not connected to Navidrome" in response.json()["detail"]


def test_connect_failure(client, monkeypatch):
    """Failed connection should return 400 with error message."""
    mock_client = AsyncMock()
    mock_client.ping.side_effect = Exception("Connection refused")

    import app.routers.auth as auth_module
    from app.models.database import get_db

    # Mock DB dependency (connect endpoint now saves config to DB)
    mock_db = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    monkeypatch.setattr(auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client)

    config = NavidromeConfig(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/connect", json=config.model_dump())
    assert response.status_code == 400
    assert "Connection refused" in response.json()["detail"]

    app.dependency_overrides.pop(get_db, None)


def test_connect_success(client, monkeypatch):
    """Successful connection should return connected status and server version."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}

    import app.routers.auth as auth_module
    from app.models.database import get_db

    # Mock DB dependency (connect endpoint saves config to DB)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_db] = lambda: mock_db

    monkeypatch.setattr(auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client)

    config = NavidromeConfig(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/connect", json=config.model_dump())
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is True
    assert "Connected to Navidrome v0.50.0" in data["message"]
    assert data["server_version"] == "0.50.0"
    assert auth_module._current_client is not None
    assert auth_module._current_sync_service is not None

    app.dependency_overrides.pop(get_db, None)


def test_connect_and_sync_status(client, monkeypatch):
    """After connecting, sync-status should show Idle."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}

    import app.routers.auth as auth_module

    monkeypatch.setattr(auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client)

    # Connect first
    config = NavidromeConfig(url="http://test", username="user", password="pass")
    client.post("/api/auth/connect", json=config.model_dump())

    # Now check sync status
    response = client.get("/api/auth/sync-status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_syncing"] is False
    assert data["message"] == "Idle"


def test_sync_already_in_progress(client, monkeypatch):
    """Sync should return 409 if a sync is already running."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}

    mock_sync_service = MagicMock()
    mock_sync_service.is_syncing = True
    mock_sync_service.last_sync = None

    import app.routers.auth as auth_module

    monkeypatch.setattr(auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client)
    auth_module._current_client = mock_client
    auth_module._current_sync_service = mock_sync_service

    response = client.post("/api/auth/sync")
    assert response.status_code == 409
    assert "Sync already in progress" in response.json()["detail"]


def test_sync_success(client, monkeypatch):
    """Successful sync should return sync stats and update sync status."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}

    mock_sync_service = MagicMock()
    mock_sync_service.is_syncing = False
    mock_sync_service.last_sync = None
    mock_sync_service.full_sync = AsyncMock(
        return_value={"tracks": 10, "albums": 2, "artists": 1}
    )
    mock_sync_service.full_sync.return_value = {"tracks": 10, "albums": 2, "artists": 1}

    import app.routers.auth as auth_module
    from app.models.database import get_db

    # Override get_db dependency with a mock
    mock_db = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    monkeypatch.setattr(auth_module, "NavidromeClient", lambda *args, **kwargs: mock_client)
    auth_module._current_client = mock_client
    auth_module._current_sync_service = mock_sync_service

    response = client.post("/api/auth/sync")
    assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
    data = response.json()
    assert data["is_syncing"] is True  # Now async — returns immediately
    assert data["message"] == "Sync started"

    # Clean up dependency override
    app.dependency_overrides.pop(get_db, None)
