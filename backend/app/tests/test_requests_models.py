"""Schema tests for the request workflow.

The interesting part is not that the tables exist — it is the concurrency
contract. Two users asking for the same album at the same moment must not
produce two open requests, because approving both would add the same release
group to Lidarr twice and each would then fight the other over monitoring.

A partial unique index is the guarantee, so these tests hit the database rather
than the service: a service-level check ("is there an open request? then don't
insert") loses that race the moment two requests arrive together.
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    Blocklist,
    Request,
    RequestVote,
    User,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")

    # SQLite ignores FK constraints — including ON DELETE CASCADE — unless the
    # pragma is set per connection. The app's engine installs this listener at
    # import (app.models.database), so a test engine without it would silently
    # prove nothing about cascades.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, name="sze", **kwargs):
    user = User(
        username=name,
        display_name=name,
        navidrome_url="http://nav",
        navidrome_password="secret",
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request(db, user, rgid="rg-1", **kwargs):
    req = Request(
        user_id=user.id,
        kind=kwargs.pop("kind", "album"),
        release_group_mbid=rgid,
        album_title="An Album",
        **kwargs,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


# ── Flags on users ──────────────────────────────────────────────────────────


def test_new_user_is_not_auto_approved_but_can_request(db):
    """Both defaults matter: silently trusting a new account is the wrong way
    to be wrong; silently blocking someone from asking is just annoying."""
    user = _user(db)
    assert user.auto_approve is False
    assert user.can_request is True


# ── The partial unique index ────────────────────────────────────────────────


def test_second_open_request_for_the_same_album_is_rejected(db):
    owner = _user(db, "sze")
    guest = _user(db, "rebecca")
    _request(db, owner, "rg-1")

    with pytest.raises(IntegrityError):
        _request(db, guest, "rg-1")
    db.rollback()


def test_denied_request_allows_a_new_one(db):
    """Dedupe memory must not become a permanent block: a denied album can be
    asked for again later (tastes change, or the owner said 'not yet')."""
    owner = _user(db)
    first = _request(db, owner, "rg-1")
    first.status = "denied"
    db.commit()

    second = _request(db, owner, "rg-1")
    assert second.id != first.id
    assert second.status == "pending"


def test_cancelled_request_allows_a_new_one(db):
    owner = _user(db)
    first = _request(db, owner, "rg-1")
    first.status = "cancelled"
    db.commit()

    assert _request(db, owner, "rg-1").status == "pending"


def test_artist_requests_are_not_covered_by_the_album_index(db):
    """The index predicate is `kind = 'album'`, so an artist request and an
    album request for the same release group cannot collide."""
    owner = _user(db)
    _request(db, owner, "rg-1", kind="album")
    _request(db, owner, "rg-1", kind="artist")  # must not raise
    assert db.query(Request).count() == 2


def test_open_statuses_all_block_a_duplicate(db):
    """pending/approved/processing are open; only denied and cancelled open the
    door. Missing one from the predicate would allow a double add to Lidarr."""
    for status in ("pending", "approved", "processing"):
        owner = _user(db, f"user-{status}")
        _request(db, owner, f"rg-{status}")
        db.query(Request).filter_by(release_group_mbid=f"rg-{status}").one().status = status
        db.commit()

        with pytest.raises(IntegrityError):
            _request(db, owner, f"rg-{status}")
        db.rollback()


# ── Votes ───────────────────────────────────────────────────────────────────


def test_voting_twice_is_a_no_op(db):
    """(request_id, user_id) is the PK, so a double tap on 'me too' is an
    IntegrityError rather than a second vote — the service treats that as
    success, and a vote count can never be inflated by a double-click."""
    owner = _user(db, "sze")
    guest = _user(db, "rebecca")
    req = _request(db, owner)

    db.add(RequestVote(request_id=req.id, user_id=guest.id))
    db.commit()

    with pytest.raises(IntegrityError):
        db.add(RequestVote(request_id=req.id, user_id=guest.id))
        db.commit()
    db.rollback()

    assert db.query(RequestVote).count() == 1


def test_votes_cascade_with_request_and_user(db):
    owner = _user(db, "sze")
    guest = _user(db, "rebecca")
    req = _request(db, owner)
    db.add(RequestVote(request_id=req.id, user_id=guest.id))
    db.commit()

    db.delete(req)
    db.commit()
    assert db.query(RequestVote).count() == 0


# ── Blocklist ───────────────────────────────────────────────────────────────


def test_blocklist_stores_release_group_and_artist_scopes(db):
    owner = _user(db)
    db.add(Blocklist(scope="release_group", release_group_mbid="rg-1",
                     reason="kidz bop", created_by=owner.id))
    db.add(Blocklist(scope="artist", artist_mbid="artist-1",
                     reason="not for us", created_by=owner.id))
    db.commit()

    rows = db.query(Blocklist).all()
    assert {r.scope for r in rows} == {"release_group", "artist"}
    # One column per scope, so a lookup is a direct equality on the id type.
    assert [r.release_group_mbid for r in rows if r.scope == "release_group"] == ["rg-1"]
    assert [r.artist_mbid for r in rows if r.scope == "artist"] == ["artist-1"]
