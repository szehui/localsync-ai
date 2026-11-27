"""Tests for full-sync pruning of vanished library rows.

Navidrome can reassign song IDs on a re-scan. Before pruning existed, the old
rows stayed forever and polluted tracks.search_key, which the ListenBrainz
resolver uses to map listens to Navidrome IDs.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import Artist, Album, Base, Track, User
from app.services.sync import SyncService, _prune


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")

    # Mirror production so an annotation whose user or track is missing fails
    # loudly here instead of silently in prod.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        User(id=1, username="sze", navidrome_url="https://navidrome.example.com",
             navidrome_password="secret")
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _track(tid, title="T", artist="A"):
    return Track(id=tid, title=title, artist_name=artist, search_key=f"{artist}|{title}".lower())


class TestPruneHelper:
    def test_deletes_rows_not_seen(self, db):
        db.add_all([_track("keep"), _track("stale")])
        db.commit()

        deleted = _prune(db, Track, {"keep"})

        assert deleted == 1
        assert db.query(Track).count() == 1
        assert db.query(Track).first().id == "keep"

    def test_keeps_everything_when_all_seen(self, db):
        db.add_all([_track("a"), _track("b")])
        db.commit()
        assert _prune(db, Track, {"a", "b"}) == 0
        assert db.query(Track).count() == 2

    def test_empty_seen_set_never_wipes_the_library(self, db):
        """A failed fetch must not nuke local metadata."""
        db.add_all([_track("a"), _track("b")])
        db.commit()
        assert _prune(db, Track, set()) == 0
        assert db.query(Track).count() == 2

    def test_handles_more_rows_than_the_parameter_limit(self, db):
        # SQLite caps bound parameters; batching must handle >500 stale rows.
        db.add_all([_track(f"stale{i}") for i in range(1200)])
        db.add(_track("keep"))
        db.commit()

        assert _prune(db, Track, {"keep"}) == 1200
        assert db.query(Track).count() == 1


class TestFullSyncPrune:
    def _client(self, artist_ids, albums, album_songs):
        client = MagicMock()
        client.get_artists = AsyncMock(
            return_value=[{"id": a, "name": f"Artist {a}"} for a in artist_ids]
        )
        client.get_album_list2 = AsyncMock(
            return_value=[
                {"id": a, "name": f"Album {a}", "songCount": len(album_songs.get(a, []))}
                for a in albums
            ]
        )

        async def get_album(album_id):
            return {"album": {"name": f"Album {album_id}", "song": album_songs.get(album_id, [])}}

        client.get_album = get_album
        return client

    @pytest.mark.asyncio
    async def test_stale_track_is_removed_on_resync(self, db):
        """Simulates Navidrome reassigning a song ID between syncs."""
        db.add_all([_track("old-song-id", title="Bag", artist="Lil' Kim")])
        db.commit()

        client = self._client(
            ["ar1"], ["al1"],
            {"al1": [{"id": "new-song-id", "title": "Bag", "artist": "Lil' Kim",
                      "album": "9", "parent": "al1"}]},
        )
        svc = SyncService(client, user_id=1)

        stats = await svc.full_sync(db)

        ids = [t.id for t in db.query(Track).all()]
        assert ids == ["new-song-id"]
        assert stats["pruned_tracks"] == 1
        assert stats["tracks"] == 1

    @pytest.mark.asyncio
    async def test_stale_album_and_artist_removed(self, db):
        db.add_all([
            Artist(id="gone-artist", name="Gone"),
            Album(id="gone-album", name="Gone"),
            _track("t1"),
        ])
        db.commit()

        client = self._client(
            ["ar1"], ["al1"],
            {"al1": [{"id": "t1", "title": "T", "artist": "A", "album": "Al1", "parent": "al1"}]},
        )
        svc = SyncService(client, user_id=1)

        stats = await svc.full_sync(db)

        assert db.query(Artist).count() == 1
        assert db.query(Album).count() == 1
        assert stats["pruned_artists"] == 1
        assert stats["pruned_albums"] == 1
        # The surviving track was seen, so it stays.
        assert db.query(Track).count() == 1

    @pytest.mark.asyncio
    async def test_no_prune_when_navidrome_returns_nothing(self, db):
        """Degraded upstream must not erase the local library."""
        db.add_all([_track("t1"), _track("t2")])
        db.commit()

        client = self._client([], [], {})
        svc = SyncService(client, user_id=1)

        stats = await svc.full_sync(db)

        assert db.query(Track).count() == 2
        assert stats["pruned_tracks"] == 0

    @pytest.mark.asyncio
    async def test_recording_mbid_and_search_key_populated(self, db):
        client = self._client(
            ["ar1"], ["al1"],
            {"al1": [{"id": "s1", "title": "Some Resolve", "artist": "Röyksopp",
                      "album": "Senior", "parent": "al1",
                      "musicBrainzId": "mbid-123"}]},
        )
        svc = SyncService(client, user_id=1)

        await svc.full_sync(db)

        track = db.query(Track).first()
        assert track.recording_mbid == "mbid-123"
        assert track.search_key == "royksopp|someresolve"
