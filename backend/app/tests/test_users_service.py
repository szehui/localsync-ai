"""Tests for the per-user client registry and credential lookup service.

Runs against a real in-memory SQLite instance for the DB-backed paths, and
against a stubbed NavidromeClient for the connection paths — no network.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    LastfmUserConfig,
    ListenbrainzUserConfig,
    User,
)
from app.services import users as users_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    # Mirror production: SQLite only honors ON DELETE CASCADE with this pragma.
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


@pytest.fixture(autouse=True)
def clean_registry():
    """Registry lives at module scope, so leaks between tests would be real."""
    users_service._clients.clear()
    users_service._sync_services.clear()
    yield
    users_service._clients.clear()
    users_service._sync_services.clear()


def run(coro):
    """Drive one coroutine on a private loop.

    Deliberately not ``asyncio.run``: that unsets the process-wide current loop
    on exit, and other test modules here still call ``asyncio.get_event_loop()``
    and would then fail depending on collection order.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_user(db, username="sze", url="https://navidrome.example.com", password="secret"):
    user = User(username=username, navidrome_url=url, navidrome_password=password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestGetOrCreateUser:
    def test_get_or_create_user_is_stable(self, db):
        first = users_service.get_or_create_user(
            db, "https://navidrome.example.com", "sze", "first-pass"
        )
        second = users_service.get_or_create_user(
            db, "https://navidrome.example.com", "sze", "second-pass"
        )

        assert first.id == second.id
        assert db.query(User).count() == 1
        # A successful login is the only password-change signal we get.
        assert second.navidrome_password == "second-pass"
        assert db.get(User, first.id).navidrome_password == "second-pass"

    def test_get_or_create_user_sets_login_timestamp(self, db):
        user = users_service.get_or_create_user(db, "https://navidrome.example.com", "sze", "pw")
        assert user.last_login is not None

    def test_second_user_does_not_touch_the_first(self, db):
        sze = users_service.get_or_create_user(
            db, "https://navidrome.example.com", "sze", "sze-pass"
        )
        rebecca = users_service.get_or_create_user(
            db, "https://navidrome.example.com", "rebecca", "rebecca-pass"
        )

        assert rebecca.id != sze.id
        refreshed = db.get(User, sze.id)
        assert refreshed.navidrome_password == "sze-pass"
        assert refreshed.username == "sze"

    def test_same_username_different_server_is_refused(self, db):
        """Reverses the earlier (url, username) contract, on production evidence.

        Same server at a different address produced an empty duplicate tenant
        with none of the user's history. Refusing is recoverable; see
        UsernameOnOtherUrl.
        """
        users_service.get_or_create_user(db, "http://a", "sze", "pw")
        with pytest.raises(users_service.UsernameOnOtherUrl) as exc:
            users_service.get_or_create_user(db, "http://b", "sze", "pw")

        assert exc.value.existing_url == "http://a"
        assert db.query(User).count() == 1

    def test_different_username_on_different_server_is_allowed(self, db):
        """Two real servers still work, as long as the usernames differ."""
        a = users_service.get_or_create_user(db, "http://a", "sze", "pw")
        b = users_service.get_or_create_user(db, "http://b", "rebecca", "pw")

        assert a.id != b.id
        assert db.query(User).count() == 2


class TestClientRegistry:
    def test_client_registry_is_per_user(self, db):
        users_service.set_client_for_user(1, MagicMock())
        users_service.set_client_for_user(2, MagicMock())

        assert users_service.peek_client(1) is not None
        assert users_service.peek_client(2) is not None
        assert users_service.peek_client(1) is not users_service.peek_client(2)

        client = MagicMock()
        client.close = AsyncMock()
        users_service.set_client_for_user(1, client)
        run(users_service.clear_clients_for_user(1))

        assert users_service.peek_client(1) is None
        assert users_service.peek_client(2) is not None
        client.close.assert_awaited_once()

    def test_set_client_stores_sync_service(self):
        service = MagicMock()
        users_service.set_client_for_user(1, MagicMock(), sync_service=service)
        assert users_service.peek_sync_service(1) is service

    def test_clear_clients_drops_sync_service(self):
        users_service.set_client_for_user(1, MagicMock(), sync_service=MagicMock())
        run(users_service.clear_clients_for_user(1))
        assert users_service.peek_sync_service(1) is None

    def test_clear_clients_survives_close_failure(self):
        client = MagicMock()
        client.close = AsyncMock(side_effect=RuntimeError("socket already gone"))
        users_service.set_client_for_user(1, client)

        run(users_service.clear_clients_for_user(1))

        assert users_service.peek_client(1) is None


class TestGetClient:
    def test_get_client_caches_and_reuses(self, db, monkeypatch):
        make_user(db)
        created = []

        class StubClient:
            def __init__(self, url="", username="", password=""):
                self.url, self.username, self.password = url, username, password
                self.pings = 0
                created.append(self)

            async def ping(self):
                self.pings += 1
                return {"status": "ok"}

            async def close(self):
                pass

        monkeypatch.setattr(users_service, "NavidromeClient", StubClient)

        first = run(users_service.get_client(db, 1))
        second = run(users_service.get_client(db, 1))

        assert first is second
        assert len(created) == 1
        assert first.pings == 2  # once to connect, once to validate the cache

    def test_get_client_replaces_stale_client(self, db, monkeypatch):
        make_user(db)

        class FlakyClient:
            def __init__(self, url="", username="", password=""):
                self.fail = False

            async def ping(self):
                if self.fail:
                    raise RuntimeError("connection reset")
                return {"status": "ok"}

            async def close(self):
                pass

        monkeypatch.setattr(users_service, "NavidromeClient", FlakyClient)

        stale = FlakyClient()
        stale.fail = True
        users_service.set_client_for_user(1, stale)

        fresh = run(users_service.get_client(db, 1))

        assert fresh is not stale
        assert users_service.peek_client(1) is fresh

    def test_get_client_raises_for_unknown_user(self, db, monkeypatch):
        class StubClient:
            def __init__(self, **kwargs):
                pass

        monkeypatch.setattr(users_service, "NavidromeClient", StubClient)

        with pytest.raises(RuntimeError):
            run(users_service.get_client(db, 99))


class TestBuildClient:
    def test_build_client_raises_for_unknown_user(self, db):
        with pytest.raises(RuntimeError):
            users_service.build_client(db, 99)

    def test_build_client_uses_stored_credentials(self, db, monkeypatch):
        user = make_user(db, username="rebecca", url="http://nav.example", password="pw-1")
        captured = {}

        class StubClient:
            def __init__(self, url="", username="", password=""):
                captured.update(url=url, username=username, password=password)

        monkeypatch.setattr(users_service, "NavidromeClient", StubClient)

        users_service.build_client(db, user.id)

        assert captured == {
            "url": user.navidrome_url,
            "username": user.username,
            "password": user.navidrome_password,
        }

    def test_build_client_does_not_use_the_registry(self, db, monkeypatch):
        make_user(db)

        class StubClient:
            def __init__(self, **kwargs):
                pass

        monkeypatch.setattr(users_service, "NavidromeClient", StubClient)

        cached = MagicMock()
        users_service.set_client_for_user(1, cached)

        # Scheduler path builds its own client rather than borrowing the
        # logged-in one, so a logout or restart cannot break background jobs.
        assert users_service.build_client(db, 1) is not cached
        assert users_service.peek_client(1) is cached


class TestCredentialLookups:
    def test_credential_lookups_are_scoped(self, db):
        make_user(db, username="sze")
        make_user(db, username="rebecca")

        db.add(ListenbrainzUserConfig(user_id=1, user_token="lb-token", username="sze"))
        db.add(LastfmUserConfig(user_id=1, api_key="lf-key", username="sze"))
        db.commit()

        assert users_service.get_listenbrainz_config(db, 1).user_token == "lb-token"
        assert users_service.get_lastfm_config(db, 1).api_key == "lf-key"

        assert users_service.get_listenbrainz_config(db, 2) is None
        assert users_service.get_lastfm_config(db, 2) is None

    def test_get_user_returns_none_for_unknown_id(self, db):
        assert users_service.get_user(db, 99) is None
