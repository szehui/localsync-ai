"""Endpoints for notifications and the blocklist.

The permissions and the credential rule are the point:

* the webhook URL is write-only — `GET` returns a mask, and a `PUT` without a
  URL keeps the stored one, so a UI round-trip cannot destroy a working webhook;
* both routers are owner-only, and a member gets 403 rather than a partial view.
"""
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, Blocklist, NotificationConfig, User, get_db
from app.routers.auth import get_current_user
from app.services import notifier

WEBHOOK = ("https://discord.com/api/webhooks/123456789012345678/"
           "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP")


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
    guest = User(username="rebecca", display_name="Rebecca",
                 navidrome_url="http://nav", navidrome_password="x")
    db.add_all([owner, guest])
    db.commit()
    return owner, guest


@pytest.fixture
def client(db, people):
    owner, _ = people
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": owner.id, "username": owner.username,
        "navidrome_url": owner.navidrome_url,
    }
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_guest(people):
    guest = people[1]
    app.dependency_overrides[get_current_user] = lambda: {
        "id": guest.id, "username": guest.username,
        "navidrome_url": guest.navidrome_url,
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── Notifications ───────────────────────────────────────────────────────────


class TestNotificationEndpoints:
    def test_unconfigured_state(self, client):
        body = client.get("/api/notifications").json()
        assert body["configured"] is False
        assert body["available_events"] == list(notifier.EVENTS)

    def test_put_stores_and_masks(self, client, db):
        response = client.put("/api/notifications", json={
            "webhook_url": WEBHOOK, "enabled": True,
            "events": ["request.available"],
            "base_url": "https://ls.example.org",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["events"] == ["request.available"]
        assert body["base_url"] == "https://ls.example.org"
        # The token must not be in the response, in any form.
        assert "abcdefghijklmnop" not in response.text
        assert WEBHOOK not in response.text

        stored = db.query(NotificationConfig).filter(NotificationConfig.id == 1).one()
        assert stored.webhook_url == WEBHOOK

    def test_get_never_returns_the_token(self, client):
        client.put("/api/notifications", json={"webhook_url": WEBHOOK,
                                               "enabled": True})
        response = client.get("/api/notifications")
        assert "abcdefghijklmnop" not in response.text
        assert response.json()["webhook_hint"].endswith("/…")

    def test_paused_config_still_reports_its_preference(self, client):
        """Pausing must not blank the event list, or resuming sends nothing."""
        client.put("/api/notifications", json={
            "webhook_url": WEBHOOK, "enabled": True,
            "events": ["request.available"],
        })
        paused = client.put("/api/notifications", json={"enabled": False}).json()
        assert paused["enabled"] is False
        assert paused["events"] == ["request.available"]

    def test_put_without_url_keeps_the_stored_one(self, client, db):
        client.put("/api/notifications", json={"webhook_url": WEBHOOK,
                                               "enabled": True})
        # This is what a UI does when it saves the form without retyping the
        # URL: it must not write the mask over the real value.
        response = client.put("/api/notifications", json={
            "enabled": True, "events": ["request.created", "request.available"],
        })
        assert response.status_code == 200
        stored = db.query(NotificationConfig).filter(NotificationConfig.id == 1).one()
        assert stored.webhook_url == WEBHOOK
        assert stored.events == json.dumps(["request.created", "request.available"])

    def test_enabling_without_a_url_is_refused(self, client):
        response = client.put("/api/notifications", json={"enabled": True})
        assert response.status_code == 400
        assert "webhook URL" in response.json()["detail"]

    def test_unknown_event_is_refused(self, client):
        response = client.put("/api/notifications", json={
            "webhook_url": WEBHOOK, "enabled": True, "events": ["request.exploded"],
        })
        assert response.status_code == 400
        assert "request.exploded" in response.json()["detail"]

    def test_non_http_url_is_refused(self, client):
        response = client.put("/api/notifications", json={
            "webhook_url": "ftp://example.org/hook", "enabled": True,
        })
        assert response.status_code == 400

    def test_delete_clears(self, client, db):
        client.put("/api/notifications", json={"webhook_url": WEBHOOK,
                                               "enabled": True})
        assert client.delete("/api/notifications").json()["configured"] is False
        assert db.query(NotificationConfig).count() == 0

    def test_member_is_forbidden(self, client, as_guest):
        assert client.get("/api/notifications").status_code == 403
        assert client.put("/api/notifications", json={
            "webhook_url": WEBHOOK, "enabled": True}).status_code == 403

    def test_test_endpoint_reports_a_real_failure(self, client):
        """A deleted webhook must not be reported as a success."""
        client.put("/api/notifications", json={"webhook_url": WEBHOOK,
                                               "enabled": True})
        fake = AsyncMock(return_value=False)
        with patch.object(notifier, "post_webhook", fake):
            body = client.post("/api/notifications/test").json()
        assert body["sent"] is False
        assert "refused" in body["detail"]

    def test_test_endpoint_success(self, client):
        client.put("/api/notifications", json={"webhook_url": WEBHOOK,
                                               "enabled": True})
        with patch.object(notifier, "post_webhook", AsyncMock(return_value=True)):
            body = client.post("/api/notifications/test").json()
        assert body["sent"] is True

    def test_test_endpoint_without_config(self, client):
        assert client.post("/api/notifications/test").status_code == 400


# ── Blocklist ───────────────────────────────────────────────────────────────


class TestBlocklistEndpoints:
    RG = "11111111-1111-1111-1111-111111111111"

    def test_empty_list(self, client):
        body = client.get("/api/blocklist").json()
        assert body["entries"] == []
        assert body["total"] == 0

    def test_add_and_list(self, client):
        created = client.post("/api/blocklist", json={
            "scope": "release_group", "release_group_mbid": self.RG,
            "artist_name": "Daft Punk", "album_title": "Random Access Memories",
            "reason": "no",
        })
        assert created.status_code == 201
        body = client.get("/api/blocklist").json()
        assert body["total"] == 1
        assert body["entries"][0]["album_title"] == "Random Access Memories"
        assert body["entries"][0]["created_by"] == "Sze"

    def test_add_artist(self, client):
        created = client.post("/api/blocklist", json={
            "scope": "artist", "artist_mbid": "artist-1", "artist_name": "Nickelback",
        })
        assert created.status_code == 201
        assert created.json()["scope"] == "artist"

    def test_duplicate_is_409(self, client):
        payload = {"scope": "release_group", "release_group_mbid": self.RG}
        assert client.post("/api/blocklist", json=payload).status_code == 201
        second = client.post("/api/blocklist", json=payload)
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "duplicate"

    def test_missing_mbid_is_400(self, client):
        response = client.post("/api/blocklist", json={"scope": "release_group"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "missing_mbid"

    def test_scope_filter(self, client):
        client.post("/api/blocklist", json={"scope": "release_group",
                                            "release_group_mbid": self.RG})
        client.post("/api/blocklist", json={"scope": "artist",
                                            "artist_mbid": "artist-1"})
        assert client.get("/api/blocklist?scope=artist").json()["total"] == 1
        assert client.get("/api/blocklist?scope=release_group").json()["total"] == 1

    def test_delete(self, client, db):
        created = client.post("/api/blocklist", json={
            "scope": "release_group", "release_group_mbid": self.RG}).json()
        assert client.delete(f"/api/blocklist/{created['id']}").status_code == 200
        assert db.query(Blocklist).count() == 0

    def test_delete_unknown_is_404(self, client):
        assert client.delete("/api/blocklist/999").status_code == 404

    def test_member_cannot_read(self, client, as_guest):
        assert client.get("/api/blocklist").status_code == 403

    def test_member_cannot_write(self, client, as_guest):
        response = client.post("/api/blocklist", json={
            "scope": "release_group", "release_group_mbid": self.RG})
        assert response.status_code == 403

    def test_member_cannot_delete(self, client, people, as_guest, db):
        owner = people[0]
        db.add(Blocklist(scope="release_group", release_group_mbid=self.RG,
                         created_by=owner.id))
        db.commit()
        row = db.query(Blocklist).one()
        assert client.delete(f"/api/blocklist/{row.id}").status_code == 403
