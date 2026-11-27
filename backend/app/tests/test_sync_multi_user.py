"""Per-user annotation capture in the sync service.

Play counts, ratings, stars, and last-played timestamps belong to one Subsonic
account, not to the shared library row, so sync must write them to
TrackAnnotation keyed by (user_id, track_id). These tests pin that split: the
regression that matters is one user's sync overwriting another user's numbers.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Track, TrackAnnotation, User
from app.services.sync import (
    SyncService,
    _annotation_from_song,
    _build_track,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    # Mirror production: SQLite only honors FKs (and their cascades) with this.
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


def make_user(db, user_id, username):
    db.add(
        User(
            id=user_id,
            username=username,
            navidrome_url="https://navidrome.example.com",
            navidrome_password="secret",
        )
    )


def make_track(track_id="t1"):
    return Track(id=track_id, title="Track 1", artist_name="Artist", search_key="artist|track1")


class TestAnnotationIsPerUser:
    def test_annotation_is_scoped_to_user(self, db):
        make_user(db, 1, "sze")
        make_user(db, 2, "alex")
        db.add(make_track("t1"))
        db.commit()

        db.merge(_annotation_from_song(1, {"id": "t1", "playCount": 40}))
        db.merge(_annotation_from_song(2, {"id": "t1", "playCount": 2}))
        db.commit()

        rows = {
            a.user_id: a.play_count
            for a in db.query(TrackAnnotation).filter(TrackAnnotation.track_id == "t1")
        }
        assert rows == {1: 40, 2: 2}
        assert db.query(TrackAnnotation).count() == 2

    def test_annotation_carries_all_play_state(self, db):
        make_user(db, 1, "sze")
        db.add(make_track("t1"))
        db.commit()

        db.merge(
            _annotation_from_song(
                1,
                {
                    "id": "t1",
                    "playCount": 12,
                    "rating": 4,
                    "starred": "2026-10-02T01:22:45Z",
                    "played": "2026-10-02T01:22:45.697086102Z",
                },
            )
        )
        db.commit()

        ann = db.query(TrackAnnotation).one()
        assert ann.play_count == 12
        assert ann.rating == 4
        assert ann.starred is True
        assert ann.last_played_at is not None
        assert ann.last_played_at.tzinfo is None


class TestBuildTrackIsMetadataOnly:
    def test_build_track_no_longer_carries_play_state(self):
        song = {
            "id": "t1",
            "title": "Track 1",
            "artist": "Artist",
            "album": "Album",
            "parent": "al1",
            "playCount": 99,
            "rating": 5,
            "starred": "2026-10-02T01:22:45Z",
            "played": "2026-10-02T01:22:45Z",
        }

        track = _build_track(song, {"name": "Album"})

        # Library metadata still lands on the Track.
        assert track.id == "t1"
        assert track.title == "Track 1"
        assert track.album_name == "Album"
        # Play state must not be taken from the song object. The legacy columns
        # still exist, so accept whatever the column default yields.
        assert track.play_count in (None, 0)
        assert track.rating in (None, 0)
        assert track.starred in (None, False)
        assert track.last_played_at is None
        assert track.play_count != 99
        assert track.rating != 5


class TestUpdatePlayCounts:
    def _client(self, songs):
        client = AsyncMock()
        client.get_album_list2 = AsyncMock(return_value=[{"id": "al1"}])
        client.get_album = AsyncMock(return_value={"album": {"song": songs}})
        return client

    @pytest.mark.asyncio
    async def test_update_play_counts_only_touches_own_user(self, db):
        make_user(db, 1, "sze")
        make_user(db, 2, "alex")
        db.add(make_track("t1"))
        db.commit()

        db.add(TrackAnnotation(user_id=1, track_id="t1", play_count=5))
        db.add(TrackAnnotation(user_id=2, track_id="t1", play_count=7))
        db.commit()

        client = self._client([{"id": "t1", "playCount": 99}])
        svc = SyncService(client, user_id=1)

        await svc._update_play_counts(db)
        db.commit()

        mine = db.query(TrackAnnotation).filter(TrackAnnotation.user_id == 1).one()
        theirs = db.query(TrackAnnotation).filter(TrackAnnotation.user_id == 2).one()
        assert mine.play_count == 99
        assert theirs.play_count == 7
        assert db.query(TrackAnnotation).count() == 2

    @pytest.mark.asyncio
    async def test_update_play_counts_skips_unknown_tracks(self, db):
        """An FK violation would abort the batch, so unknown songs are skipped."""
        make_user(db, 1, "sze")
        db.commit()

        client = self._client([{"id": "ghost", "playCount": 3}])
        svc = SyncService(client, user_id=1)

        await svc._update_play_counts(db)
        db.commit()

        assert db.query(TrackAnnotation).count() == 0
