"""Ownership isolation for the Smart Triggers REST API.

Triggers are per-tenant rows: `GET/PUT/DELETE/POST /api/triggers` must only ever
see the caller's own rows, and another user's trigger must look like it does not
exist (404, never 403 — a 403 would confirm the id is real).

Runs against real in-memory SQLite (a mock cannot express a foreign-key owner
join), with the JWT login path stubbed at the token layer: the identity the API
sees is exactly what `get_current_user` derives from the token.
"""
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.main import app
from app.models.database import Base, SmartTrigger, User, get_db


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    """Real in-memory SQLite session, also installed as the get_db override.

    StaticPool keeps every thread on one connection — TestClient dispatches the
    request from its own thread — so the app sees the same in-memory database as
    the test. PRAGMA foreign_keys mirrors production, where the owner FK is real.
    """
    engine = create_engine(
        "sqlite:///:memory:",
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
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def stub_scheduler(monkeypatch):
    """No-op the APScheduler calls the router makes.

    The router imports the job helpers by name, so they are patched in the
    router's namespace. Without this every request would reach for a scheduler
    instance that only exists after app startup.
    """
    import app.routers.triggers as triggers_module

    for name in (
        "add_trigger_job",
        "remove_trigger_job",
        "pause_trigger_job",
        "resume_trigger_job",
    ):
        monkeypatch.setattr(triggers_module, name, lambda *a, **kw: None)


def _login(db, username="user", url="http://test"):
    """Create the tenant and return (token, uid) — the /login result, minus the
    Navidrome round-trip.

    Mirrors app/tests/test_auth.py's login helper: the token carries the same
    sub/url/uid claims `POST /api/auth/login` mints, and the User row it names
    exists in the database the API is reading.
    """
    user = User(
        username=username,
        display_name=username,
        navidrome_url=url,
        navidrome_password="pass",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = jwt.encode(
        {"sub": username, "url": url, "uid": user.id, "exp": 9999999999},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token, user.id


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create(client, token, name="My Trigger", trigger_type="recency"):
    response = client.post(
        "/api/triggers/",
        json={"name": name, "trigger_type": trigger_type},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_triggers_are_not_visible_across_users(client, db):
    """A's trigger appears in A's list and nowhere in B's."""
    token_a, _uid_a = _login(db, "alice")
    token_b, _uid_b = _login(db, "bob")

    created = _create(client, token_a, name="Alice Mix")

    list_a = client.get("/api/triggers/", headers=_auth(token_a))
    assert list_a.status_code == 200
    assert [t["id"] for t in list_a.json()] == [created["id"]]

    list_b = client.get("/api/triggers/", headers=_auth(token_b))
    assert list_b.status_code == 200
    assert list_b.json() == []


def test_user_cannot_update_another_users_trigger(client, db):
    """B's PUT is a 404 and leaves A's row untouched."""
    token_a, _uid_a = _login(db, "alice")
    token_b, _uid_b = _login(db, "bob")
    created = _create(client, token_a, name="Alice Mix")

    response = client.put(
        f"/api/triggers/{created['id']}",
        json={"name": "Stolen", "enabled": False},
        headers=_auth(token_b),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Trigger not found"

    row = db.query(SmartTrigger).filter(SmartTrigger.id == created["id"]).one()
    assert row.name == "Alice Mix"
    assert row.enabled is True


def test_user_cannot_delete_another_users_trigger(client, db):
    """B's DELETE is a 404 and the row survives."""
    token_a, _uid_a = _login(db, "alice")
    token_b, _uid_b = _login(db, "bob")
    created = _create(client, token_a, name="Alice Mix")

    response = client.delete(
        f"/api/triggers/{created['id']}", headers=_auth(token_b)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Trigger not found"

    assert db.query(SmartTrigger).filter(SmartTrigger.id == created["id"]).count() == 1


def test_user_cannot_toggle_another_users_trigger(client, db):
    """B's toggle is a 404 and A's enabled flag is unchanged."""
    token_a, _uid_a = _login(db, "alice")
    token_b, _uid_b = _login(db, "bob")
    created = _create(client, token_a, name="Alice Mix")

    response = client.post(
        f"/api/triggers/{created['id']}/toggle", headers=_auth(token_b)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Trigger not found"

    row = db.query(SmartTrigger).filter(SmartTrigger.id == created["id"]).one()
    assert row.enabled is True


def test_created_trigger_records_owner(client, db):
    """The new row is stamped with the caller's tenant id."""
    token_a, uid_a = _login(db, "alice")

    created = _create(client, token_a, name="Alice Mix")

    row = db.query(SmartTrigger).filter(SmartTrigger.id == created["id"]).one()
    assert row.owner_id == uid_a


def test_list_returns_only_own_triggers(client, db):
    """Each list carries exactly its owner's rows: A has two, B has one."""
    token_a, uid_a = _login(db, "alice")
    token_b, uid_b = _login(db, "bob")

    _create(client, token_a, name="A one")
    _create(client, token_a, name="A two")
    _create(client, token_b, name="B one")

    list_a = client.get("/api/triggers/", headers=_auth(token_a)).json()
    list_b = client.get("/api/triggers/", headers=_auth(token_b)).json()

    assert len(list_a) == 2
    assert len(list_b) == 1
    assert {t["name"] for t in list_a} == {"A one", "A two"}
    assert [t["name"] for t in list_b] == ["B one"]
    assert db.query(SmartTrigger).filter(SmartTrigger.owner_id == uid_a).count() == 2
    assert db.query(SmartTrigger).filter(SmartTrigger.owner_id == uid_b).count() == 1
