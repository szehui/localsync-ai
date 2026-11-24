"""Tests for album-level Lidarr adds.

The critical behaviour under test is the monitoring collapse: Lidarr's
POST /api/v1/album monitors the artist's whole discography regardless of the
artist's addOptions.monitor, so every add must be followed by un-monitoring the
siblings. Verified live against Lidarr 3.1.6.5079 (7/7 Strokes albums monitored).
"""
import pytest

from app.services.lidarr import LidarrService

LOOKUP_RESULT = [
    {
        "title": "Room at the Top",
        "foreignAlbumId": "wrong-rg",
        "artist": {"artistName": "Other", "foreignArtistId": "other-artist"},
        "releases": [],
    },
    {
        "title": "Room on Fire",
        "foreignAlbumId": "rg-room-on-fire",
        "artist": {"artistName": "The Strokes", "foreignArtistId": "strokes-mbid"},
        "releases": [{"title": "US CD"}],
    },
]

DEFAULTS = {
    "quality_profile_id": 1,
    "metadata_profile_id": 1,
    "root_folder": "/data/music",
}


def make_service(handler):
    """LidarrService with _request replaced by `handler(method, endpoint, **kw)`."""
    service = LidarrService("http://lidarr.test", "key")
    calls = []

    async def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return handler(method, endpoint, **kwargs)

    service._request = fake_request  # type: ignore[assignment]
    return service, calls


class TestPickAlbum:
    def test_exact_title_wins_over_order(self):
        service = LidarrService("http://x", "k")
        picked = service._pick_album(LOOKUP_RESULT, "Room on Fire")
        assert picked["foreignAlbumId"] == "rg-room-on-fire"

    def test_containment_fallback(self):
        service = LidarrService("http://x", "k")
        rows = [{"title": "Room on Fire (Deluxe Edition)", "foreignAlbumId": "d"}]
        assert service._pick_album(rows, "Room on Fire")["foreignAlbumId"] == "d"

    def test_first_row_when_nothing_matches(self):
        service = LidarrService("http://x", "k")
        rows = [{"title": "Totally Different", "foreignAlbumId": "a"},
                {"title": "Also Different", "foreignAlbumId": "b"}]
        assert service._pick_album(rows, "Room on Fire")["foreignAlbumId"] == "a"

    def test_empty(self):
        service = LidarrService("http://x", "k")
        assert service._pick_album([], "anything") is None


class TestAddSingleAlbum:
    async def test_payload_injects_profile_fields_and_unmonitors_artist(self):
        def handler(method, endpoint, **kwargs):
            if endpoint == "qualityprofile":
                return [{"id": 1, "name": "Any"}]
            if endpoint == "metadataprofile":
                return [{"id": 1, "name": "Standard"}]
            if endpoint == "rootfolder":
                return [{"path": "/data/music"}]
            if endpoint == "album/lookup":
                return LOOKUP_RESULT
            if endpoint == "album" and method == "GET" and "foreignAlbumId" in kwargs.get("params", {}):
                return []  # not already present
            if endpoint == "album" and method == "POST":
                return {"id": 3863, "artistId": 474,
                        "artist": {"artistName": "The Strokes"}}
            if endpoint == "album" and method == "GET":
                return [
                    {"id": 3863, "monitored": True, "title": "Room on Fire"},
                    {"id": 3864, "monitored": True, "title": "Angles"},
                    {"id": 3865, "monitored": True, "title": "Is This It"},
                ]
            if endpoint == "album/monitor":
                return {"ok": True}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, calls = make_service(handler)
        result = await service.add_single_album("The Strokes", "Room on Fire")

        assert result["album_id"] == 3863
        assert result["already_present"] is False
        assert sorted(result["albums_unmonitored"]) == [3864, 3865]

        post = next(c for c in calls if c[1] == "album" and c[0] == "POST")
        payload = post[2]["json"]
        # album is wanted
        assert payload["monitored"] is True
        assert payload["addOptions"] == {"addType": "manual", "searchForNewAlbum": False}
        assert payload["foreignAlbumId"] == "rg-room-on-fire"
        # artist is NOT subscribed, and lookup omitted these fields
        artist = payload["artist"]
        assert artist["monitored"] is False
        assert artist["addOptions"]["monitor"] == "none"
        assert artist["addOptions"]["searchForMissingAlbums"] is False
        assert artist["qualityProfileId"] == 1
        assert artist["metadataProfileId"] == 1
        assert artist["rootFolderPath"] == "/data/music"
        # collapse happened, keeping only the target album
        monitor = next(c for c in calls if c[1] == "album/monitor")
        assert monitor[2]["json"] == {"albumIds": [3864, 3865], "monitored": False}

    async def test_already_present_short_circuits(self):
        def handler(method, endpoint, **kwargs):
            if endpoint == "qualityprofile":
                return [{"id": 1}]
            if endpoint == "metadataprofile":
                return [{"id": 1}]
            if endpoint == "rootfolder":
                return [{"path": "/data/music"}]
            if endpoint == "album/lookup":
                return LOOKUP_RESULT
            if endpoint == "album" and method == "GET":
                return [{"id": 999, "artistId": 1, "title": "Room on Fire",
                         "artist": {"artistName": "The Strokes"}}]
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, calls = make_service(handler)
        result = await service.add_single_album("The Strokes", "Room on Fire")

        assert result["already_present"] is True
        assert result["album_id"] == 999
        assert not any(c[0] == "POST" and c[1] == "album" for c in calls)

    async def test_search_flag_triggers_album_search(self):
        def handler(method, endpoint, **kwargs):
            if endpoint == "qualityprofile":
                return [{"id": 1}]
            if endpoint == "metadataprofile":
                return [{"id": 1}]
            if endpoint == "rootfolder":
                return [{"path": "/data/music"}]
            if endpoint == "album/lookup":
                return LOOKUP_RESULT
            if endpoint == "album" and method == "GET" and "foreignAlbumId" in kwargs.get("params", {}):
                return []
            if endpoint == "album" and method == "POST":
                return {"id": 3863, "artistId": 474,
                        "artist": {"artistName": "The Strokes"}}
            if endpoint == "album" and method == "GET":
                return [{"id": 3863, "monitored": True, "title": "Room on Fire"}]
            if endpoint == "album/monitor":
                return {}
            if endpoint == "command":
                return {"id": 7, "name": "AlbumSearch"}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, calls = make_service(handler)
        await service.add_single_album("The Strokes", "Room on Fire", search=True)

        post = next(c for c in calls if c[1] == "album" and c[0] == "POST")
        assert post[2]["json"]["addOptions"]["searchForNewAlbum"] is True
        command = next(c for c in calls if c[1] == "command")
        assert command[2]["json"] == {"name": "AlbumSearch", "albumIds": [3863]}

    async def test_missing_release_name_rejected(self):
        service = LidarrService("http://x", "k")
        with pytest.raises(ValueError, match="artist and a release name"):
            await service.add_single_album("Artist", "")

    async def test_no_defaults_raises(self):
        def handler(method, endpoint, **kwargs):
            if endpoint in ("qualityprofile", "metadataprofile", "rootfolder"):
                return []
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, _ = make_service(handler)
        with pytest.raises(ValueError, match="quality profile"):
            await service.add_single_album("Artist", "Album")


class TestCollapseArtistMonitoring:
    async def test_unmonitors_everything_except_target(self):
        def handler(method, endpoint, **kwargs):
            if endpoint == "album":
                return [
                    {"id": 1, "monitored": True},
                    {"id": 2, "monitored": True},
                    {"id": 3, "monitored": False},
                ]
            if endpoint == "album/monitor":
                return {}
            raise AssertionError

        service, calls = make_service(handler)
        unset = await service.collapse_artist_monitoring(474, keep_album_id=1)
        assert unset == [2]
        monitor = next(c for c in calls if c[1] == "album/monitor")
        assert monitor[2]["json"] == {"albumIds": [2], "monitored": False}

    async def test_noop_when_already_single(self):
        def handler(method, endpoint, **kwargs):
            if endpoint == "album":
                return [{"id": 1, "monitored": True}, {"id": 2, "monitored": False}]
            raise AssertionError("should not call album/monitor")

        service, calls = make_service(handler)
        assert await service.collapse_artist_monitoring(474, keep_album_id=1) == []
        assert not any(c[1] == "album/monitor" for c in calls)
