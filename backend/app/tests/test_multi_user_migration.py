"""Tests for the one-time multi-user migration in init_db().

Exercises _migrate_multi_user against a real in-memory SQLite instance so the
FKs, the composite annotation PK, and the UPDATE ... WHERE owner_id IS NULL
backfill all really run.
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    ConnectionConfig,
    GeneratedPlaylist,
    LastfmConfig,
    ListenbrainzConfig,
    ListenbrainzUserConfig,
    SmartTrigger,
    Track,
    TrackAnnotation,
    User,
    _migrate_multi_user,
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


def seed_legacy_connection(db, **kwargs):
    conn = ConnectionConfig(
        id=1,
        url=kwargs.get("url", "https://navidrome.example.com"),
        username=kwargs.get("username", "sze"),
        password=kwargs.get("password", "secret"),
    )
    db.add(conn)
    db.commit()
    return conn


def test_legacy_connection_becomes_owner_user(db):
    seed_legacy_connection(db)
    db.add(ListenbrainzConfig(id=1, user_token="lb-token", username="sze"))
    # t1 carries real legacy state; t2 is all-zero and must NOT be copied.
    db.add(Track(id="t1", title="Song One", play_count=17, starred=True, rating=4))
    db.add(Track(id="t2", title="Song Two", play_count=0, rating=0))
    db.commit()

    _migrate_multi_user(db)

    users = db.query(User).all()
    assert len(users) == 1
    owner = users[0]
    assert owner.is_owner is True
    assert owner.username == "sze"
    assert owner.id == 1
    assert owner.display_name == "sze"
    assert owner.navidrome_url == "https://navidrome.example.com"
    assert owner.navidrome_password == "secret"

    annotations = db.query(TrackAnnotation).all()
    assert len(annotations) == 1, "all-zero track rows must be skipped"
    ann = annotations[0]
    assert ann.user_id == owner.id
    assert ann.track_id == "t1"
    assert ann.play_count == 17
    assert ann.rating == 4
    assert ann.starred is True
    assert db.query(TrackAnnotation).filter_by(track_id="t2").first() is None

    lb_rows = db.query(ListenbrainzUserConfig).all()
    assert len(lb_rows) == 1
    assert lb_rows[0].user_id == owner.id
    assert lb_rows[0].user_token == "lb-token"
    assert lb_rows[0].username == "sze"


def test_migration_is_idempotent(db):
    seed_legacy_connection(db)
    db.add(Track(id="t1", title="Song One", play_count=5, rating=3))
    db.commit()

    _migrate_multi_user(db)
    annotations_after_first = db.query(TrackAnnotation).count()

    _migrate_multi_user(db)

    assert db.query(User).count() == 1
    assert db.query(TrackAnnotation).count() == annotations_after_first
    assert db.query(TrackAnnotation).count() == 1


def test_no_legacy_config_is_a_noop(db):
    _migrate_multi_user(db)  # must not raise
    assert db.query(User).count() == 0
    assert db.query(TrackAnnotation).count() == 0


def test_legacy_triggers_get_owner_backfilled(db):
    owner = User(
        username="sze",
        navidrome_url="https://navidrome.example.com",
        navidrome_password="secret",
        is_owner=True,
    )
    db.add(owner)
    db.commit()

    trigger = SmartTrigger(name="Recency", trigger_type="recency", owner_id=None)
    playlist = GeneratedPlaylist(name="Mix", track_ids="[]", owner_id=None)
    db.add_all([trigger, playlist])
    db.commit()

    _migrate_multi_user(db)

    db.expire_all()
    assert db.get(SmartTrigger, trigger.id).owner_id == owner.id
    assert db.get(GeneratedPlaylist, playlist.id).owner_id == owner.id
