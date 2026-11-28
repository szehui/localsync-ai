"""Tests for the duplicate-username login guard.

A Navidrome server is reachable under more than one address, and the tenant key
is (url, username). Without this guard, logging in at a second address silently
minted a second account: no play history, no discovery seeds, no playlists, and
nothing in the UI to explain it. These tests pin the refusal instead.
"""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, User, get_db
from app.services.users import UsernameOnOtherUrl, get_or_create_user

PUBLIC = "https://navidrome.hui-family.org"
LAN = "http://192.168.4.205:4533"


@pytest.fixture
def db():
    # StaticPool keeps one connection so the in-memory DB is visible from the
    # worker thread TestClient runs the app on -- otherwise the app sees an
    # empty database and every request fails with "no such table".
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    app.dependency_overrides.pop(get_db, None)


def seed_user(db, url=PUBLIC, username="sze", password="pw", owner=True):
    row = User(username=username, navidrome_url=url, navidrome_password=password,
               display_name=username, is_owner=owner)
    db.add(row)
    db.commit()
    return row


class TestGetOrCreateUser:
    def test_same_url_username_returns_existing_row(self, db):
        existing = seed_user(db)
        user = get_or_create_user(db, PUBLIC, "sze", "new-password")
        assert user.id == existing.id
        assert user.navidrome_password == "new-password"

    def test_new_username_on_new_url_is_created(self, db):
        seed_user(db, url=PUBLIC, username="sze")
        other = get_or_create_user(db, LAN, "rebecca", "pw")
        assert other.id != 1
        assert other.navidrome_url == LAN

    def test_same_username_on_other_url_is_refused(self, db):
        seed_user(db)
        with pytest.raises(UsernameOnOtherUrl) as exc:
            get_or_create_user(db, LAN, "sze", "pw")
        assert exc.value.existing_url == PUBLIC
        assert "sze" in str(exc.value)

    def test_refusal_creates_no_row(self, db):
        seed_user(db)
        with pytest.raises(UsernameOnOtherUrl):
            get_or_create_user(db, LAN, "sze", "pw")
        db.rollback()
        assert db.query(User).count() == 1

    def test_error_message_names_the_url_to_use(self, db):
        seed_user(db)
        with pytest.raises(UsernameOnOtherUrl) as exc:
            get_or_create_user(db, LAN, "sze", "pw")
        assert PUBLIC in str(exc.value)
        assert "Log in with that address" in str(exc.value)


class TestLoginEndpoint:
    def _patch_ping(self, monkeypatch):
        import app.routers.auth as auth_module

        mock_client = AsyncMock()
        mock_client.ping.return_value = {"version": "0.54.0"}
        monkeypatch.setattr(
            auth_module, "NavidromeClient", lambda *a, **k: mock_client)
        return mock_client

    def test_conflicting_url_returns_409_with_reason(self, client, db, monkeypatch):
        seed_user(db)
        self._patch_ping(monkeypatch)
        app.dependency_overrides[get_db] = lambda: db

        response = client.post("/api/auth/login", json={
            "url": LAN, "username": "sze", "password": "pw"})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert PUBLIC in detail
        assert "already registered" in detail

    def test_same_url_still_logs_in(self, client, db, monkeypatch):
        seed_user(db)
        self._patch_ping(monkeypatch)
        app.dependency_overrides[get_db] = lambda: db

        response = client.post("/api/auth/login", json={
            "url": PUBLIC, "username": "sze", "password": "pw"})

        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_bad_credentials_still_401(self, client, db, monkeypatch):
        seed_user(db)
        import app.routers.auth as auth_module
        mock_client = AsyncMock()
        mock_client.ping.side_effect = RuntimeError("401 from Navidrome")
        monkeypatch.setattr(
            auth_module, "NavidromeClient", lambda *a, **k: mock_client)
        app.dependency_overrides[get_db] = lambda: db

        response = client.post("/api/auth/login", json={
            "url": LAN, "username": "sze", "password": "wrong"})

        assert response.status_code == 401
