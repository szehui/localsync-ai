"""The library listing must report the CALLER's play counts, not dead columns.

`tracks.play_count` / `.rating` / `.starred` stopped being written when sync
moved annotations into `track_annotations`. Anything still reading those columns
therefore reports zeros for every track — silently, since the query succeeds.

These tests pin the fix: the listing joins the caller's annotations, and a
second user sees their own numbers (or zeros) for the very same track.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock

from app.main import app
from app.models.database import Base, Track, TrackAnnotation, User, get_db
from app.services.auth import create_access_token


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def make_user(db, username, uid=None):
    user = User(
        id=uid, username=username, display_name=username,
        navidrome_url="http://nav", navidrome_password="pw",
    )
    db.add(user)
    db.commit()
    return user


def auth_header(user):
    token = create_access_token(
        data={"sub": user.username, "url": user.navidrome_url, "uid": user.id}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def two_users_one_track(db):
    """Same song, wildly different listening for each user."""
    a = make_user(db, "sze", uid=1)
    b = make_user(db, "rebecca", uid=2)
    db.add(Track(id="t1", title="Song One", artist_name="Artist A"))
    db.commit()
    db.add(TrackAnnotation(user_id=a.id, track_id="t1", play_count=42, rating=5,
                           starred=True))
    # B has no annotation row at all — the LEFT JOIN case.
    db.commit()
    return a, b


def test_listing_reports_callers_play_count(client, two_users_one_track):
    a, b = two_users_one_track

    mine = client.get("/api/library/tracks", headers=auth_header(a)).json()
    assert len(mine) == 1
    assert mine[0]["play_count"] == 42
    assert mine[0]["rating"] == 5
    assert mine[0]["starred"] is True

    theirs = client.get("/api/library/tracks", headers=auth_header(b)).json()
    assert len(theirs) == 1, "a never-played track must still be listed"
    assert theirs[0]["play_count"] == 0
    assert theirs[0]["rating"] == 0
    assert theirs[0]["starred"] is False


def test_sort_by_play_count_uses_annotations(client, two_users_one_track, db):
    a, b = two_users_one_track
    # A second track that A has never played but B has.
    db.add(Track(id="t2", title="Song Two", artist_name="Artist B"))
    db.commit()
    db.add(TrackAnnotation(user_id=b.id, track_id="t2", play_count=99))
    db.commit()

    mine = client.get("/api/library/tracks?sort=play_count",
                      headers=auth_header(a)).json()
    assert [t["id"] for t in mine] == ["t1", "t2"], "A's most-played first"

    theirs = client.get("/api/library/tracks?sort=play_count",
                        headers=auth_header(b)).json()
    assert [t["id"] for t in theirs] == ["t2", "t1"], "B's most-played first"


def test_single_track_endpoint_is_annotated_per_user(client, two_users_one_track):
    a, b = two_users_one_track
    assert client.get("/api/library/tracks/t1", headers=auth_header(a)).json()["play_count"] == 42
    assert client.get("/api/library/tracks/t1", headers=auth_header(b)).json()["play_count"] == 0


def test_missing_track_is_404(client, two_users_one_track):
    a, _ = two_users_one_track
    assert client.get("/api/library/tracks/nope", headers=auth_header(a)).status_code == 404
