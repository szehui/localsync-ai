"""Upgrade-path tests: a pre-multi-user database must survive init_db().

`Base.metadata.create_all()` creates missing TABLES but never alters existing
ones. So on a deployed database every table already exists *without* the new
columns, and only the explicit ALTER TABLE steps in `init_db()` can add them.

The unit tests elsewhere build a fresh schema from the models, which is exactly
the case where this class of bug is invisible — a fresh DB has `owner_id` from
the moment `create_all()` runs. These tests build the OLD schema by hand, then
run the real `init_db()` against it.

Regression being pinned: `init_db()` raised
`OperationalError: no such column: owner_id` on any upgraded database, which
meant the container crash-looped on startup and never served a request.
"""
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import database as db_module


# The schema as it existed immediately before multi-user support: no owner_id on
# either table, and the singleton config tables still authoritative.
LEGACY_SCHEMA = """
CREATE TABLE connection_config (
    id INTEGER PRIMARY KEY, url VARCHAR NOT NULL,
    username VARCHAR NOT NULL, password VARCHAR NOT NULL
);
CREATE TABLE listenbrainz_config (
    id INTEGER PRIMARY KEY, user_token VARCHAR NOT NULL, username VARCHAR NOT NULL,
    api_base VARCHAR, labs_api_base VARCHAR, enabled BOOLEAN,
    last_connect_check DATETIME
);
CREATE TABLE lastfm_config (
    id INTEGER PRIMARY KEY, api_key VARCHAR NOT NULL, username VARCHAR NOT NULL
);
CREATE TABLE tracks (
    id VARCHAR PRIMARY KEY, title VARCHAR NOT NULL, album_id VARCHAR,
    album_name VARCHAR, artist_id VARCHAR, artist_name VARCHAR, genre VARCHAR,
    year INTEGER, duration INTEGER, track_number INTEGER, disc_number INTEGER,
    play_count INTEGER, rating INTEGER, starred BOOLEAN, created_at DATETIME,
    last_synced DATETIME, last_played_at DATETIME, recording_mbid VARCHAR,
    search_key VARCHAR
);
CREATE TABLE smart_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR NOT NULL,
    trigger_type VARCHAR NOT NULL, enabled BOOLEAN, cron_expression VARCHAR,
    threshold INTEGER, playlist_name VARCHAR, navidrome_playlist_id VARCHAR,
    last_run DATETIME, next_run DATETIME, created_at DATETIME, mix_kind VARCHAR
);
CREATE TABLE generated_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR NOT NULL,
    navidrome_playlist_id VARCHAR, seed_track_id VARCHAR, seed_track_name VARCHAR,
    seed_playlist_id VARCHAR, seed_playlist_name VARCHAR, strictness INTEGER,
    track_count INTEGER, track_ids TEXT, created_at DATETIME, updated_at DATETIME
);
INSERT INTO connection_config (id, url, username, password)
    VALUES (1, 'http://nav:4533', 'sze', 'secret');
INSERT INTO listenbrainz_config (id, user_token, username)
    VALUES (1, 'lb-token', 'sze_lb');
INSERT INTO tracks (id, title, artist_name, play_count, rating, starred)
    VALUES ('t1', 'Played Song', 'Artist A', 17, 4, 1);
INSERT INTO tracks (id, title, artist_name, play_count, rating, starred)
    VALUES ('t2', 'Untouched', 'Artist A', 0, 0, 0);
INSERT INTO smart_triggers (name, trigger_type, enabled)
    VALUES ('Legacy Recency', 'recency', 1);
INSERT INTO generated_playlists (name, track_count)
    VALUES ('Legacy Mix', 12);
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A legacy-schema database file wired into the real init_db()."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    yield path, engine
    engine.dispose()


def _columns(engine, table):
    insp = sa.inspect(engine)
    return [c["name"] for c in insp.get_columns(table)]


def _indexes(engine, table):
    insp = sa.inspect(engine)
    return [i["name"] for i in insp.get_indexes(table)]


def test_init_db_upgrades_legacy_database_without_crashing(legacy_db):
    """The bug: this raised OperationalError('no such column: owner_id')."""
    path, engine = legacy_db
    db_module.init_db()  # must not raise

    assert "owner_id" in _columns(engine, "smart_triggers")
    assert "owner_id" in _columns(engine, "generated_playlists")
    # The model's index=True is not applied by ALTER TABLE, so init_db must
    # create it explicitly or upgraded installs quietly lose the index.
    assert "ix_smart_triggers_owner_id" in _indexes(engine, "smart_triggers")
    assert "ix_generated_playlists_owner_id" in _indexes(engine, "generated_playlists")


def test_init_db_adopts_legacy_data_on_upgrade(legacy_db):
    path, engine = legacy_db
    db_module.init_db()

    Session = sessionmaker(bind=engine)
    with Session() as db:
        users = db.query(db_module.User).all()
        assert len(users) == 1
        owner = users[0]
        assert owner.username == "sze"
        assert owner.is_owner is True

        # Only the played track is annotated; the all-zero row is skipped.
        anns = db.query(db_module.TrackAnnotation).all()
        assert {a.track_id for a in anns} == {"t1"}
        assert anns[0].play_count == 17

        # Legacy triggers and playlists must become reachable again.
        trigger = db.query(db_module.SmartTrigger).one()
        playlist = db.query(db_module.GeneratedPlaylist).one()
        assert trigger.owner_id == owner.id
        assert playlist.owner_id == owner.id

        # Credentials carried over to the per-user tables.
        lb = db.query(db_module.ListenbrainzUserConfig).one()
        assert lb.user_id == owner.id


def test_init_db_is_idempotent_on_second_startup(legacy_db):
    """A restart (or a crash mid-migration) must not raise or duplicate."""
    path, engine = legacy_db
    db_module.init_db()
    db_module.init_db()

    Session = sessionmaker(bind=engine)
    with Session() as db:
        assert db.query(db_module.User).count() == 1
        assert db.query(db_module.TrackAnnotation).count() == 1


def test_legacy_columns_and_tables_survive_the_upgrade(legacy_db):
    """They are the rollback path — dropping them early removes the way back."""
    path, engine = legacy_db
    db_module.init_db()

    insp = sa.inspect(engine)
    assert "connection_config" in insp.get_table_names()
    assert "listenbrainz_config" in insp.get_table_names()
    assert "lastfm_config" in insp.get_table_names()
    assert {"play_count", "rating", "starred", "last_played_at"} <= set(
        _columns(engine, "tracks")
    )
