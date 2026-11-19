"""Tests for the ListenBrainz config endpoints."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import get_db
from app.models.schemas import LoginRequest


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_globals():
    import app.routers.auth as auth_module

    auth_module._current_client = None
    auth_module._current_sync_service = None
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_navidrome_client():
    mock_client = AsyncMock()
    mock_client.ping.return_value = {"version": "0.50.0"}
    return mock_client


def _login(client, monkeypatch, mock_navidrome_client):
    """Log in and return the JWT. Also yields the DB mock used for auth."""
    import app.routers.auth as auth_module

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    monkeypatch.setattr(
        auth_module, "NavidromeClient", lambda *args, **kwargs: mock_navidrome_client
    )
    app.dependency_overrides[get_db] = lambda: mock_db

    creds = LoginRequest(url="http://test", username="user", password="pass")
    response = client.post("/api/auth/login", json=creds.model_dump())
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


class TestAuthRequired:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/auth/listenbrainz"),
        ("PUT", "/api/auth/listenbrainz"),
        ("DELETE", "/api/auth/listenbrainz"),
    ])
    def test_requires_token(self, client, method, path):
        response = client.request(method, path)
        assert response.status_code == 401


class TestGetConfig:
    def test_not_configured(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[get_db] = lambda: db

        response = client.get("/api/auth/listenbrainz", headers=_headers(token))
        assert response.status_code == 200
        assert response.json() == {"configured": False, "username": None}

    def test_configured_hides_token(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)

        row = MagicMock()
        row.username = "szeis4cookie"
        row.user_token = "super-secret-token"
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        app.dependency_overrides[get_db] = lambda: db

        response = client.get("/api/auth/listenbrainz", headers=_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["username"] == "szeis4cookie"
        # The token must never leave the server.
        assert "super-secret-token" not in response.text
        assert "token" not in body


class TestPutConfig:
    def test_creates_when_absent(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[get_db] = lambda: db

        response = client.put(
            "/api/auth/listenbrainz",
            headers=_headers(token),
            json={"username": "szeis4cookie", "token": "tok123"},
        )
        assert response.status_code == 200
        assert response.json() == {"configured": True, "username": "szeis4cookie"}
        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_updates_when_present(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)

        row = MagicMock()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        app.dependency_overrides[get_db] = lambda: db

        response = client.put(
            "/api/auth/listenbrainz",
            headers=_headers(token),
            json={"username": "newname", "token": "newtok"},
        )
        assert response.status_code == 200
        assert row.username == "newname"
        assert row.user_token == "newtok"
        db.commit.assert_called_once()

    def test_missing_fields_rejected(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        app.dependency_overrides[get_db] = lambda: db

        response = client.put(
            "/api/auth/listenbrainz", headers=_headers(token), json={"username": "x"},
        )
        assert response.status_code == 422


class TestDeleteConfig:
    def test_clears_config(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)

        db = MagicMock()
        app.dependency_overrides[get_db] = lambda: db

        response = client.delete("/api/auth/listenbrainz", headers=_headers(token))
        assert response.status_code == 200
        assert response.json() == {"configured": False, "username": None}
        db.query.return_value.filter.return_value.delete.assert_called_once()
        db.commit.assert_called_once()


class TestTestEndpoint:
    def _config_row(self):
        row = MagicMock()
        row.username = "szeis4cookie"
        row.user_token = "tok"
        return row

    def test_requires_config(self, client, monkeypatch, mock_navidrome_client):
        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[get_db] = lambda: db

        response = client.post("/api/auth/listenbrainz/test", headers=_headers(token))
        assert response.status_code == 400

    def test_valid_token_reports_count(self, client, monkeypatch, mock_navidrome_client):
        import app.routers.auth as auth_module

        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self._config_row()
        app.dependency_overrides[get_db] = lambda: db

        lb = AsyncMock()
        lb.validate_token.return_value = {"valid": True, "user_name": "szeis4cookie"}
        lb.get_listen_count.return_value = 0
        monkeypatch.setattr(auth_module, "ListenBrainzClient", lambda **kw: lb)

        response = client.post("/api/auth/listenbrainz/test", headers=_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is True
        assert body["listen_count"] == 0
        assert body["username"] == "szeis4cookie"
        lb.close.assert_awaited()

    def test_rejected_token(self, client, monkeypatch, mock_navidrome_client):
        import app.routers.auth as auth_module

        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self._config_row()
        app.dependency_overrides[get_db] = lambda: db

        lb = AsyncMock()
        lb.validate_token.return_value = {"valid": False, "message": "Invalid token"}
        monkeypatch.setattr(auth_module, "ListenBrainzClient", lambda **kw: lb)

        response = client.post("/api/auth/listenbrainz/test", headers=_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert "rejected" in body["message"].lower()

    def test_unknown_user(self, client, monkeypatch, mock_navidrome_client):
        import app.routers.auth as auth_module
        from app.services.listenbrainz import UserNotFoundError

        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self._config_row()
        app.dependency_overrides[get_db] = lambda: db

        lb = AsyncMock()
        lb.validate_token.return_value = {"valid": True}
        lb.get_listen_count.side_effect = UserNotFoundError("nope")
        monkeypatch.setattr(auth_module, "ListenBrainzClient", lambda **kw: lb)

        response = client.post("/api/auth/listenbrainz/test", headers=_headers(token))
        body = response.json()
        assert body["valid"] is False
        assert "not found" in body["message"].lower()

    def test_network_error_does_not_500(self, client, monkeypatch, mock_navidrome_client):
        import app.routers.auth as auth_module
        from app.services.listenbrainz import ListenBrainzError

        token = _login(client, monkeypatch, mock_navidrome_client)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self._config_row()
        app.dependency_overrides[get_db] = lambda: db

        lb = AsyncMock()
        lb.validate_token.side_effect = ListenBrainzError("connection reset")
        monkeypatch.setattr(auth_module, "ListenBrainzClient", lambda **kw: lb)

        response = client.post("/api/auth/listenbrainz/test", headers=_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert "connection reset" in body["message"]
