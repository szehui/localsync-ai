"""Tests for the multi-tenant User and TrackAnnotation models.

Runs against a real in-memory SQLite instance with the actual models — not
mocks — so the composite PK, FK cascade, and unique constraint are really
exercised.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    Track,
    User,
    TrackAnnotation,
    GeneratedPlaylist,
    SmartTrigger,
    ListenbrainzUserConfig,
    LastfmUserConfig,
)


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


def make_user(db, user_id=1, username="sze", url="https://navidrome.example.com"):
    user = User(id=user_id, username=username, navidrome_url=url,
                navidrome_password="secret")
    db.add(user)
    return user


class TestUser:
    def test_create_user_with_defaults(self, db):
        user = User(
            username="sze",
            navidrome_url="https://navidrome.example.com",
            navidrome_password="secret",
        )
        db.add(user)
        db.commit()
        assert user.id is not None
        assert user.display_name is None
        assert user.is_owner is False
        assert isinstance(user.created_at, datetime)
        assert user.last_login is None

    def test_unique_navidrome_url_username(self, db):
        db.add(User(
            username="sze",
            navidrome_url="https://navidrome.example.com",
            navidrome_password="secret",
        ))
        db.commit()
        db.add(User(
            username="sze",
            navidrome_url="https://navidrome.example.com",
            navidrome_password="other",
        ))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_same_username_different_url_is_allowed(self, db):
        db.add(User(
            username="sze",
            navidrome_url="https://navidrome.example.com",
            navidrome_password="secret",
        ))
        db.commit()
        db.add(User(
            username="sze",
            navidrome_url="https://other.example.com",
            navidrome_password="secret",
        ))
        db.commit()  # must not raise

    def test_required_fields_are_not_null(self, db):
        with pytest.raises(IntegrityError):
            db.add(User(username="sze", navidrome_password="secret"))  # no url
            db.commit()

    def test_owner_flag(self, db):
        user = User(
            username="admin",
            navidrome_url="https://navidrome.example.com",
            navidrome_password="secret",
            is_owner=True,
        )
        db.add(user)
        db.commit()
        assert user.is_owner is True


class TestTrackAnnotation:
    def test_create_annotation_with_defaults(self, db):
        make_user(db, user_id=1)
        db.add(Track(id="t1", title="Song One"))
        db.commit()
        ann = TrackAnnotation(user_id=1, track_id="t1")
        db.add(ann)
        db.commit()
        assert ann.play_count == 0
        assert ann.rating == 0
        assert ann.starred is False
        assert ann.last_played_at is None
        assert isinstance(ann.updated_at, datetime)

    def test_composite_pk(self, db):
        db.add(Track(id="t1", title="Song One"))
        make_user(db, user_id=1)
        db.commit()
        db.add(TrackAnnotation(user_id=1, track_id="t1", rating=5, starred=True))
        db.add(TrackAnnotation(user_id=1, track_id="t1", rating=1))  # same pair
        with pytest.raises(IntegrityError):
            db.commit()

    def test_same_track_different_users_is_allowed(self, db):
        db.add(Track(id="t1", title="Song One"))
        make_user(db, user_id=1, username="a")
        make_user(db, user_id=2, username="b")
        db.commit()
        db.add(TrackAnnotation(user_id=1, track_id="t1", rating=5))
        db.add(TrackAnnotation(user_id=2, track_id="t1", rating=1))
        db.commit()  # must not raise
        assert db.query(TrackAnnotation).filter_by(track_id="t1").count() == 2

    def test_fk_cascade_delete_user(self, db):
        make_user(db, user_id=1)
        db.add(Track(id="t1", title="Song One"))
        db.commit()
        db.add(TrackAnnotation(user_id=1, track_id="t1", rating=4))
        db.commit()
        db.query(User).filter_by(id=1).delete()
        db.commit()
        remaining = db.query(TrackAnnotation).filter_by(user_id=1).all()
        assert remaining == []

    def test_fk_cascade_delete_track(self, db):
        make_user(db, user_id=1)
        db.add(Track(id="t1", title="Song One"))
        db.commit()
        db.add(TrackAnnotation(user_id=1, track_id="t1", rating=4))
        db.commit()
        db.query(Track).filter_by(id="t1").delete()
        db.commit()
        remaining = db.query(TrackAnnotation).filter_by(track_id="t1").all()
        assert remaining == []

    def test_unknown_user_is_rejected(self, db):
        db.add(Track(id="t1", title="Song One"))
        db.commit()
        db.add(TrackAnnotation(user_id=99, track_id="t1", rating=4))
        with pytest.raises(IntegrityError):
            db.commit()


class TestListenbrainzUserConfig:
    def test_create_listenbrainz_user_config(self, db):
        make_user(db, user_id=1)
        db.commit()
        cfg = ListenbrainzUserConfig(
            user_id=1,
            user_token="lb-token-123",
            username="sze",
        )
        db.add(cfg)
        db.commit()
        assert cfg.user_id == 1
        assert cfg.user_token == "lb-token-123"
        assert cfg.username == "sze"
        # ListenBrainz defaults come from the model, not the caller.
        assert cfg.api_base == "https://api.listenbrainz.org"
        assert cfg.labs_api_base == "https://labs.api.listenbrainz.org"
        assert cfg.enabled is True
        assert cfg.last_connect_check is None

    def test_one_config_per_user_id(self, db):
        make_user(db, user_id=1)
        db.commit()
        db.add(ListenbrainzUserConfig(user_id=1, user_token="a", username="sze"))
        db.add(ListenbrainzUserConfig(user_id=1, user_token="b", username="sze"))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_unknown_user_is_rejected(self, db):
        db.add(ListenbrainzUserConfig(user_id=99, user_token="a", username="sze"))
        with pytest.raises(IntegrityError):
            db.commit()


class TestLastfmUserConfig:
    def test_create_lastfm_user_config(self, db):
        make_user(db, user_id=1)
        db.commit()
        cfg = LastfmUserConfig(user_id=1, api_key="key-123", username="sze")
        db.add(cfg)
        db.commit()
        assert cfg.user_id == 1
        assert cfg.api_key == "key-123"
        assert cfg.username == "sze"

    def test_one_config_per_user_id(self, db):
        make_user(db, user_id=1)
        db.commit()
        db.add(LastfmUserConfig(user_id=1, api_key="a", username="sze"))
        db.add(LastfmUserConfig(user_id=1, api_key="b", username="sze"))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_unknown_user_is_rejected(self, db):
        db.add(LastfmUserConfig(user_id=99, api_key="a", username="sze"))
        with pytest.raises(IntegrityError):
            db.commit()


class TestGeneratedPlaylist:
    def test_owner_id_defaults_to_none(self, db):
        pl = GeneratedPlaylist(name="Mix", track_ids="[]")
        db.add(pl)
        db.commit()
        assert pl.owner_id is None

    def test_accepts_owner_id(self, db):
        make_user(db, user_id=1)
        db.commit()
        pl = GeneratedPlaylist(name="Mix", track_ids="[]", owner_id=1)
        db.add(pl)
        db.commit()
        assert pl.owner_id == 1

    def test_queryable_by_owner_id(self, db):
        make_user(db, user_id=1)
        make_user(db, user_id=2, username="other")
        db.commit()
        db.add(GeneratedPlaylist(name="A", track_ids="[]", owner_id=1))
        db.add(GeneratedPlaylist(name="B", track_ids="[]", owner_id=2))
        db.commit()
        mine = (
            db.query(GeneratedPlaylist)
            .filter_by(owner_id=1)
            .order_by(GeneratedPlaylist.name)
            .all()
        )
        assert [p.name for p in mine] == ["A"]


class TestSmartTrigger:
    def test_owner_id_defaults_to_none(self, db):
        tr = SmartTrigger(name="Recency", trigger_type="recency")
        db.add(tr)
        db.commit()
        assert tr.owner_id is None

    def test_accepts_owner_id(self, db):
        make_user(db, user_id=1)
        db.commit()
        tr = SmartTrigger(name="Recency", trigger_type="recency", owner_id=1)
        db.add(tr)
        db.commit()
        assert tr.owner_id == 1

    def test_queryable_by_owner_id(self, db):
        make_user(db, user_id=1)
        make_user(db, user_id=2, username="other")
        db.commit()
        db.add(SmartTrigger(name="A", trigger_type="recency", owner_id=1))
        db.add(SmartTrigger(name="B", trigger_type="heavy_rotation", owner_id=2))
        db.commit()
        mine = (
            db.query(SmartTrigger)
            .filter_by(owner_id=1)
            .order_by(SmartTrigger.name)
            .all()
        )
        assert [t.name for t in mine] == ["A"]
