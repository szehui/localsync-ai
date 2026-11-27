"""Tests for the household roster and owner-only account removal.

These endpoints exist to answer "who is on this server" and "get this person
off it", so the assertions that matter are the negative ones: a password must
never appear in a JSON body, a non-owner must never be able to delete, and a
delete must either take the right rows with it (cascade) or deliberately keep
them (playlist history).

Runs against a real in-memory SQLite database with the same foreign_keys
pragma production uses — without it, ON DELETE CASCADE silently does nothing
and every cascade assertion here would pass for the wrong reason.
"""
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import (
    Base,
    GeneratedPlaylist,
    ListenbrainzUserConfig,
    Track,
    TrackAnnotation,
    User,
    get_db,
)

# A password value distinctive enough that finding it anywhere in a response
# body is proof of a leak, not a coincidence.
NAV_PASSWORD = "hunter2-navidrome-pw"
LB_TOKEN = "lb-token-must-never-leak"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear the process-wide client registry around every test."""
    import app.services.users as users_service

    users_service._clients.clear()
    users_service._sync_services.clear()
    yield
    users_service._clients.clear()
    users_service._sync_services.clear()
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db():
    """In-memory SQLite with foreign_keys ON, installed as the get_db override.

    StaticPool keeps the request thread on the one connection that holds the
    in-memory database.
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


# ─── Helpers ───────────────────────────────────────────────────────────────


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def _token_for(uid, username="ghost", url="http://test"):
    """Mint a valid token for a tenant id (mirrors test_auth._token_for)."""
    from app.config import settings

    return jwt.encode(
        {"sub": username, "url": url, "uid": uid, "exp": 9999999999},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _make_user(db, username, is_owner=False, display_name=None):
    """Insert a User row directly — these tests are about the endpoints, not login."""
    user = User(
        username=username,
        display_name=display_name or username,
        navidrome_url="http://test",
        navidrome_password=NAV_PASSWORD,
        is_owner=is_owner,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ─── Roster ────────────────────────────────────────────────────────────────


def test_list_users_shows_roster(client, db):
    """Both accounts are listed, with ListenBrainz state per user."""
    owner = _make_user(db, "sze", is_owner=True, display_name="Sze")
    other = _make_user(db, "partner", display_name="Partner")

    db.add(ListenbrainzUserConfig(
        user_id=other.id, user_token=LB_TOKEN, username="partner-lb",
    ))
    db.commit()

    response = client.get("/api/auth/users", headers=_auth_header(_token_for(owner.id, "sze")))
    assert response.status_code == 200, response.text

    body = response.json()
    assert [u["username"] for u in body] == ["sze", "partner"]
    by_name = {u["username"]: u for u in body}

    assert by_name["sze"]["id"] == owner.id
    assert by_name["sze"]["display_name"] == "Sze"
    assert by_name["sze"]["is_owner"] is True
    assert by_name["sze"]["created_at"] is not None
    assert by_name["sze"]["last_login"] is None

    assert by_name["partner"]["is_owner"] is False

    # Exactly the linked account reports a ListenBrainz link — the boolean is
    # per-row, not a global "somebody configured it".
    assert by_name["partner"]["listenbrainz_configured"] is True
    assert by_name["sze"]["listenbrainz_configured"] is False
    assert sum(u["listenbrainz_configured"] for u in body) == 1


def test_list_users_never_exposes_secrets(client, db):
    """No credential field exists, and no stored value appears anywhere in the body."""
    owner = _make_user(db, "sze", is_owner=True)
    other = _make_user(db, "partner")
    db.add(ListenbrainzUserConfig(
        user_id=other.id, user_token=LB_TOKEN, username="partner-lb",
    ))
    db.commit()

    response = client.get("/api/auth/users", headers=_auth_header(_token_for(owner.id, "sze")))
    assert response.status_code == 200

    forbidden_keys = {"navidrome_password", "password", "user_token", "token"}
    for entry in response.json():
        leaked = forbidden_keys.intersection(entry.keys())
        assert not leaked, f"{entry['username']} exposed {leaked}"

    # The raw body check catches a future field that renames rather than
    # removes the secret — the model-shape check above cannot.
    assert NAV_PASSWORD not in response.text
    assert LB_TOKEN not in response.text
    assert "partner-lb" not in response.text  # the LB username is not roster data either


# ─── Removal: authorization ────────────────────────────────────────────────


def test_non_owner_cannot_delete(client, db):
    """A plain member deleting anyone — even a non-owner — is a 403."""
    owner = _make_user(db, "sze", is_owner=True)
    member_a = _make_user(db, "alice")
    member_b = _make_user(db, "bob")

    response = client.delete(
        f"/api/auth/users/{member_a.id}",
        headers=_auth_header(_token_for(member_b.id, "bob")),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Only the owner can remove users"
    assert db.get(User, member_a.id) is not None
    assert db.get(User, member_b.id) is not None
    assert db.get(User, owner.id) is not None


def test_owner_cannot_delete_self(client, db):
    """The owner cannot remove their own account — 400, row untouched."""
    owner = _make_user(db, "sze", is_owner=True)

    response = client.delete(
        f"/api/auth/users/{owner.id}",
        headers=_auth_header(_token_for(owner.id, "sze")),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot remove the owner account"
    assert db.get(User, owner.id) is not None


def test_owner_flag_is_read_from_db_not_token(client, db):
    """A token for an account that is not an owner buys nothing, whatever it claims.

    The caller is resolved by id from the users table; a forged or stale
    ``is_owner`` claim in the JWT must not grant deletion rights.
    """
    from app.config import settings

    victim = _make_user(db, "victim")
    attacker = _make_user(db, "attacker", is_owner=False)

    forged = jwt.encode(
        {
            "sub": "attacker", "url": "http://test", "uid": attacker.id,
            "is_owner": True, "owner": True, "exp": 9999999999,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    response = client.delete(f"/api/auth/users/{victim.id}", headers=_auth_header(forged))
    assert response.status_code == 403
    assert db.get(User, victim.id) is not None


def test_delete_unknown_user_404(client, db):
    """An id that does not exist is a 404, not a silent success."""
    owner = _make_user(db, "sze", is_owner=True)

    response = client.delete(
        "/api/auth/users/9999",
        headers=_auth_header(_token_for(owner.id, "sze")),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_users_endpoints_require_auth(client, db):
    """Both new endpoints are behind get_current_user."""
    owner = _make_user(db, "sze", is_owner=True)

    assert client.get("/api/auth/users").status_code == 401
    assert client.delete(f"/api/auth/users/{owner.id}").status_code == 401
    assert db.get(User, owner.id) is not None


# ─── Removal: effects ──────────────────────────────────────────────────────


def test_owner_deletes_other_user(client, db):
    """Owner removes a member: row gone, per-user rows cascaded, client closed."""
    import app.services.users as users_service

    owner = _make_user(db, "sze", is_owner=True)
    member = _make_user(db, "partner")

    track = Track(id="t1", title="Song")
    db.add(track)
    db.flush()
    db.add(TrackAnnotation(user_id=member.id, track_id=track.id, play_count=7))
    db.add(TrackAnnotation(user_id=owner.id, track_id=track.id, play_count=2))
    db.add(ListenbrainzUserConfig(
        user_id=member.id, user_token=LB_TOKEN, username="partner-lb",
    ))
    db.commit()

    # A live client is registered for the member, as a real session would be.
    fake_client = MagicMock()
    fake_client.close = AsyncMock()
    users_service.set_client_for_user(member.id, fake_client, MagicMock(name="svc"))

    response = client.delete(
        f"/api/auth/users/{member.id}",
        headers=_auth_header(_token_for(owner.id, "sze")),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True, "user_id": member.id}

    assert db.get(User, member.id) is None
    assert db.get(User, owner.id) is not None

    # Cascade proof: the member's annotation is gone, the owner's survives.
    remaining = db.query(TrackAnnotation).all()
    assert [(a.user_id, a.play_count) for a in remaining] == [(owner.id, 2)]
    assert db.query(TrackAnnotation).filter_by(user_id=member.id).count() == 0

    assert db.get(ListenbrainzUserConfig, member.id) is None

    # The live client was dropped before the row disappeared, not left behind.
    assert users_service.peek_client(member.id) is None
    assert users_service.peek_sync_service(member.id) is None
    fake_client.close.assert_awaited()


def test_delete_keeps_playlist_history(client, db):
    """A removed user's generated playlists survive with owner_id cleared."""
    owner = _make_user(db, "sze", is_owner=True)
    member = _make_user(db, "partner")

    playlist = GeneratedPlaylist(
        name="Daily Mix 2026-06-01", track_ids='["t1","t2"]', owner_id=member.id,
    )
    db.add(playlist)
    db.commit()
    playlist_id = playlist.id

    response = client.delete(
        f"/api/auth/users/{member.id}",
        headers=_auth_header(_token_for(owner.id, "sze")),
    )
    assert response.status_code == 200, response.text

    kept = db.get(GeneratedPlaylist, playlist_id)
    assert kept is not None, "playlist history must not be deleted with the account"
    assert kept.owner_id is None
    assert kept.name == "Daily Mix 2026-06-01"
    assert kept.track_ids == '["t1","t2"]'


def test_delete_does_not_touch_other_users_playlists(client, db):
    """Clearing owner_id is scoped to the target — another user's history stays."""
    owner = _make_user(db, "sze", is_owner=True)
    member = _make_user(db, "partner")

    theirs = GeneratedPlaylist(name="Mine", track_ids="[]", owner_id=owner.id)
    doomed = GeneratedPlaylist(name="Theirs", track_ids="[]", owner_id=member.id)
    db.add_all([theirs, doomed])
    db.commit()
    theirs_id, doomed_id = theirs.id, doomed.id

    response = client.delete(
        f"/api/auth/users/{member.id}",
        headers=_auth_header(_token_for(owner.id, "sze")),
    )
    assert response.status_code == 200

    assert db.get(GeneratedPlaylist, theirs_id).owner_id == owner.id
    assert db.get(GeneratedPlaylist, doomed_id).owner_id is None
