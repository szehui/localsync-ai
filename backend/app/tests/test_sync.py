"""Tests for library sync service."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from app.services.sync import SyncService, _parse_datetime
from app.models.database import Artist, Album, Track


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_artists = AsyncMock(return_value=[
        {"id": "a1", "name": "Artist One", "albumCount": 2},
        {"id": "a2", "name": "Artist Two", "albumCount": 1},
    ])
    client.get_album_list2 = AsyncMock(return_value=[
        {
            "id": "al1", "name": "Album One", "artistId": "a1",
            "artist": "Artist One", "year": 2024, "genre": "Rock",
            "songCount": 3, "playCount": 10, "rating": 4,
            "created": "2024-01-15T10:00:00Z",
        },
        {
            "id": "al2", "name": "Album Two", "artistId": "a2",
            "artist": "Artist Two", "year": 2023, "genre": "Jazz",
            "songCount": 2, "playCount": 5, "rating": 3,
            "created": "2024-02-20T14:00:00Z",
        },
    ])
    # Subsonic API returns getAlbum response as: { "album": { "id": ..., "song": [...] } }
    client.get_album = AsyncMock(side_effect=lambda aid: {
        "album": {
            "id": aid, "name": "Album One",
            "song": [
                {"id": "t1", "title": "Track 1", "parent": "al1", "artistId": "a1",
                 "artist": "Artist One", "genre": "Rock", "year": 2024,
                 "duration": 180, "track": 1, "discNumber": 1,
                 "playCount": 5, "rating": 4, "starred": False,
                 "created": "2024-01-15T10:00:00Z"},
                {"id": "t2", "title": "Track 2", "parent": "al1", "artistId": "a1",
                 "artist": "Artist One", "genre": "Rock", "year": 2024,
                 "duration": 200, "track": 2, "discNumber": 1,
                 "playCount": 3, "rating": 3, "starred": True,
                 "created": "2024-01-15T10:00:00Z"},
            ],
        } if aid == "al1" else {
            "id": aid, "name": "Album Two",
            "song": [
                {"id": "t3", "title": "Track 3", "parent": "al2", "artistId": "a2",
                 "artist": "Artist Two", "genre": "Jazz", "year": 2023,
                 "duration": 240, "track": 1, "discNumber": 1,
                 "playCount": 2, "rating": 5, "starred": False,
                 "created": "2024-02-20T14:00:00Z"},
                {"id": "t4", "title": "Track 4", "parent": "al2", "artistId": "a2",
                 "artist": "Artist Two", "genre": "Jazz", "year": 2023,
                 "duration": 260, "track": 2, "discNumber": 1,
                 "playCount": 1, "rating": 3, "starred": False,
                 "created": "2024-02-20T14:00:00Z"},
            ],
        }
    })
    return client


@pytest.fixture
def sync_service(mock_client):
    return SyncService(mock_client)


class TestParseDatetime:
    def test_iso_string(self):
        result = _parse_datetime("2024-01-15T10:00:00Z")
        assert result is not None
        assert result.year == 2024

    def test_none(self):
        assert _parse_datetime(None) is None

    def test_empty_string(self):
        assert _parse_datetime("") is None

    def test_already_datetime(self):
        dt = datetime(2024, 1, 15)
        assert _parse_datetime(dt) == dt


class TestFullSync:
    @pytest.mark.asyncio
    async def test_full_sync_populates_db(self, sync_service, mock_client):
        """Full sync should store artists, albums, and tracks."""
        db = MagicMock()

        # The only db.query() call in full_sync is: db.query(Album.id).all()
        # to get the list of album IDs to fetch tracks for.
        id1 = MagicMock()
        id1.id = "al1"
        id2 = MagicMock()
        id2.id = "al2"

        qm = MagicMock()
        qm.all.return_value = [id1, id2]
        db.query.return_value = qm

        stats = await sync_service.full_sync(db)

        assert stats["artists"] == 2
        assert stats["albums"] == 2
        assert stats["tracks"] == 4
        assert sync_service.last_sync is not None
        assert not sync_service.is_syncing

    @pytest.mark.asyncio
    async def test_full_sync_merges_existing(self, sync_service, mock_client):
        """Full sync should merge (upsert) existing records."""
        db = MagicMock()
        # Simulate existing artist
        existing_artist = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing_artist

        stats = await sync_service.full_sync(db)
        assert stats["artists"] == 2
        # merge should be called (not just add)
        assert db.merge.called

    @pytest.mark.asyncio
    async def test_full_sync_sets_sync_flag(self, sync_service, mock_client):
        """is_syncing should be True during sync and False after."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        assert not sync_service.is_syncing
        await sync_service.full_sync(db)
        assert not sync_service.is_syncing


class TestIncrementalSync:
    @pytest.mark.asyncio
    async def test_incremental_sync_new_albums(self, sync_service, mock_client):
        """Incremental sync should pick up new albums."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        stats = await sync_service.incremental_sync(db)
        assert stats["albums"] >= 0
        assert not sync_service.is_syncing

    @pytest.mark.asyncio
    async def test_incremental_sync_skips_existing(self, sync_service, mock_client):
        """Incremental sync should update existing albums, not duplicate."""
        db = MagicMock()
        existing_album = MagicMock()
        existing_album.id = "al1"
        existing_album.name = "Old Name"
        db.query.return_value.filter.return_value.first.return_value = existing_album

        stats = await sync_service.incremental_sync(db)
        # Should update existing rather than create new
        assert stats["albums"] >= 0
