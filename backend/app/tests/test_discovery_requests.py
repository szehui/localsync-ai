"""Discovery offer → request conversion.

The old endpoint called `add_single_album(artist_name, release_name)`, which
resolved by *title*. That is how the Shang-Chi soundtrack resolved to Savage
Mode: right artist, wrong album, queued for download. The conversion now needs a
release-group MBID, so an offer either resolves exactly or is refused.

These are the first tests this endpoint has ever had, so they also pin the
minimum: it must not add anything to Lidarr directly any more.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, DiscoveryOffer, Request, User, get_db
from app.routers.auth import get_current_user
from app.services import requests as requests_service

RG = "33333333-3333-3333-3333-333333333333"
RELEASE = "44444444-4444-4444-4444-444444444444"


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
def owner(db):
    user = User(username="sze", display_name="Sze", navidrome_url="http://nav",
                navidrome_password="x", is_owner=True, auto_approve=True)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def offer(db, owner):
    row = DiscoveryOffer(
        user_id=owner.id,
        recording_mbid="rec-1",
        artist_name="Daft Punk",
        track_title="Get Lucky",
        release_name="Random Access Memories",
        release_mbid=RELEASE,
        source="labs",
        bucket="artist_known",
        status="offered",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def client(db, owner):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": owner.id, "username": owner.username,
        "navidrome_url": owner.navidrome_url,
    }
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def lidarr(monkeypatch):
    service = MagicMock()
    service.add_albums_by_mbid = AsyncMock(return_value={
        "results": [{"release_group_mbid": RG, "album_id": 20, "artist_id": 2}],
        "added": [], "already_present": [], "unresolved": [], "failed": [],
        "monitoring_ok": True,
    })
    # A direct title-based add must never be used again on this path.
    service.add_single_album = AsyncMock(side_effect=AssertionError(
        "discovery add must not call the title-based add path"))
    monkeypatch.setattr(requests_service, "_lidarr_service", lambda db: service)
    monkeypatch.setattr(requests_service, "_cover", AsyncMock(return_value=None))
    return service


@pytest.fixture
def mb(monkeypatch):
    client = MagicMock()
    client.get_release = AsyncMock(return_value={
        "id": RELEASE, "release-group": {"id": RG}})
    client.get_release_group = AsyncMock(return_value={
        "id": RG, "title": "Random Access Memories", "primary-type": "Album",
        "secondary-types": [], "first-release-date": "2013-05-17",
        "artist-credit": [{"name": "Daft Punk", "artist": {"id": "artist-1"}}],
    })
    client.search_release_groups = AsyncMock(return_value={"release_groups": []})
    client.close = AsyncMock()
    monkeypatch.setattr(requests_service, "MusicBrainzClient", lambda: client)
    return client


def test_resolvable_offer_creates_a_request_and_marks_it_saved(
        client, db, offer, owner, lidarr, mb):
    response = client.post(f"/api/discovery/{offer.id}/add", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["already_present"] is False
    assert body["release_name"] == "Random Access Memories"

    # Exactly one request, keyed by the release group the offer resolved to.
    rows = db.query(Request).all()
    assert len(rows) == 1
    assert rows[0].release_group_mbid == RG
    assert rows[0].user_id == owner.id

    # auto_approve is on for this owner, so it went straight to Lidarr by MBID.
    lidarr.add_albums_by_mbid.assert_awaited_once_with([RG])
    lidarr.add_single_album.assert_not_awaited()

    db.refresh(offer)
    assert offer.status == "saved"
    assert offer.lidarr_album_id == 20


def test_offer_without_an_mbid_resolves_by_search_with_artist_agreement(
        client, db, offer, lidarr, mb):
    offer.release_mbid = None
    db.commit()
    mb.search_release_groups = AsyncMock(return_value={"release_groups": [{
        "id": RG, "title": "Random Access Memories", "primary-type": "Album",
        "secondary-types": [], "score": 100,
        "artist-credit": [{"name": "Daft Punk", "artist": {"id": "artist-1"}}],
    }]})

    response = client.post(f"/api/discovery/{offer.id}/add", json={})
    assert response.status_code == 200
    assert db.query(Request).one().release_group_mbid == RG


def test_a_same_titled_album_by_another_artist_is_rejected(
        client, db, offer, lidarr, mb):
    """The search hit must agree on artist, not just title."""
    offer.release_mbid = None
    db.commit()
    mb.search_release_groups = AsyncMock(return_value={"release_groups": [{
        "id": "some-other-rg", "title": "Random Access Memories",
        "primary-type": "Album", "secondary-types": [], "score": 100,
        "artist-credit": [{"name": "A Tribute Band",
                           "artist": {"id": "artist-9"}}],
    }]})

    response = client.post(f"/api/discovery/{offer.id}/add", json={})
    assert response.status_code == 422
    assert db.query(Request).count() == 0
    lidarr.add_albums_by_mbid.assert_not_awaited()
    db.refresh(offer)
    assert offer.status == "offered"      # nothing was consumed


def test_unresolvable_offer_is_422_and_adds_nothing(client, db, offer, lidarr, mb):
    mb.get_release = AsyncMock(return_value=None)
    mb.search_release_groups = AsyncMock(return_value={"release_groups": []})

    response = client.post(f"/api/discovery/{offer.id}/add", json={})

    assert response.status_code == 422
    assert "could not be resolved" in response.json()["detail"]
    assert db.query(Request).count() == 0
    lidarr.add_albums_by_mbid.assert_not_awaited()
    db.refresh(offer)
    assert offer.status == "offered"


def test_already_saved_offer_is_idempotent(client, db, offer, lidarr, mb):
    offer.status = "saved"
    offer.lidarr_album_id = 99
    db.commit()

    response = client.post(f"/api/discovery/{offer.id}/add", json={})

    assert response.status_code == 200
    assert response.json()["already_present"] is True
    assert db.query(Request).count() == 0
    lidarr.add_albums_by_mbid.assert_not_awaited()


def test_blocked_album_maps_to_403_and_keeps_the_offer(client, db, offer, owner,
                                                      lidarr, mb):
    from app.models.database import Blocklist

    db.add(Blocklist(scope="release_group", release_group_mbid=RG,
                     created_by=owner.id))
    db.commit()

    response = client.post(f"/api/discovery/{offer.id}/add", json={})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "blocked"
    db.refresh(offer)
    assert offer.status == "offered"
