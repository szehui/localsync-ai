"""Tests for album-level Lidarr adds.

The critical behaviour under test is the monitoring collapse: Lidarr's
POST /api/v1/album monitors the artist's whole discography regardless of the
artist's addOptions.monitor, so every add must be followed by un-monitoring the
siblings. Verified live against Lidarr 3.1.6.5079 (7/7 Strokes albums monitored).
"""
import pytest

from app.services import lidarr as lidarr_module
from app.services.lidarr import LidarrService


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Collapse/retry backoff must not actually sleep during tests."""
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(lidarr_module.asyncio, "sleep", fake_sleep)
    return delays

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

    def test_artist_beats_title_only_match(self):
        """The live Blur bug: title-only matching grabbed the wrong release.

        Searching "Blur Blur" returns "Essential Blur" (Blur), "Massive Blur"
        (Melissa Ferrick), etc. — but never the album "Blur" in the top rows.
        """
        service = LidarrService("http://x", "k")
        rows = [
            {"title": "Essential Blur", "foreignAlbumId": "comp",
             "artist": {"artistName": "Blur"}},
            {"title": "Massive Blur", "foreignAlbumId": "other",
             "artist": {"artistName": "Melissa Ferrick"}},
            {"title": "Modern Life Is Rubbish", "foreignAlbumId": "mlir",
             "artist": {"artistName": "Blur"}},
        ]
        picked = service._pick_album(rows, "Blur", "Blur")
        # Not another artist's release — a Blur release wins over "Massive Blur".
        assert service._row_artist(picked) == "Blur"

    def test_exact_title_from_artist_beats_fuzzy(self):
        """When the studio album IS in the results, exact title wins."""
        service = LidarrService("http://x", "k")
        rows = [
            {"title": "Essential Blur", "foreignAlbumId": "comp",
             "artist": {"artistName": "Blur"}},
            {"title": "Blur", "foreignAlbumId": "studio",
             "artist": {"artistName": "Blur"}},
        ]
        assert service._pick_album(
            rows, "Blur", "Blur")["foreignAlbumId"] == "studio"

    def test_never_selects_a_different_artist(self):
        """A wrong-artist album must never win over the right artist."""
        service = LidarrService("http://x", "k")
        rows = [
            {"title": "Halcyon", "foreignAlbumId": "impostor",
             "artist": {"artistName": "Sevenism"}},
            {"title": "Lights", "foreignAlbumId": "real",
             "artist": {"artistName": "Ellie Goulding"}},
        ]
        picked = service._pick_album(rows, "Halcyon", "Ellie Goulding")
        assert picked["foreignAlbumId"] == "real"

    def test_exact_artist_and_title_wins(self):
        service = LidarrService("http://x", "k")
        rows = [
            {"title": "Halcyon Remixed", "foreignAlbumId": "remix",
             "artist": {"artistName": "Ellie Goulding"}},
            {"title": "Halcyon", "foreignAlbumId": "studio",
             "artist": {"artistName": "Ellie Goulding"}},
        ]
        assert service._pick_album(
            rows, "Halcyon", "Ellie Goulding")["foreignAlbumId"] == "studio"

    def test_artist_folding_is_case_insensitive(self):
        service = LidarrService("http://x", "k")
        rows = [{"title": "Halcyon", "foreignAlbumId": "a",
                 "artist": {"artistName": "ELLIE GOULDING"}}]
        assert service._pick_album(
            rows, "Halcyon", "ellie goulding")["foreignAlbumId"] == "a"

    def test_title_fallback_when_no_artist_matches(self):
        """If the requested artist is absent, still return a usable guess."""
        service = LidarrService("http://x", "k")
        rows = [{"title": "Halcyon", "foreignAlbumId": "x",
                 "artist": {"artistName": "Someone Else"}}]
        assert service._pick_album(
            rows, "Halcyon", "Ellie Goulding")["foreignAlbumId"] == "x"

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
        # Albums flip to unmonitored once the PUT lands, so the converge loop
        # settles on its second pass rather than burning all attempts.
        monitored = {3864, 3865}

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
                    {"id": 3864, "monitored": 3864 in monitored, "title": "Angles"},
                    {"id": 3865, "monitored": 3865 in monitored, "title": "Is This It"},
                ]
            if endpoint == "album/monitor":
                monitored.difference_update(kwargs["json"]["albumIds"])
                return {"ok": True}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, calls = make_service(handler)
        result = await service.add_single_album("The Strokes", "Room on Fire")

        assert result["album_id"] == 3863
        assert result["already_present"] is False
        assert sorted(result["albums_unmonitored"]) == [3864, 3865]
        assert result["monitoring_ok"] is True

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
        # Without this Lidarr's RefreshArtist re-monitors every album it creates.
        assert artist["monitorNewItems"] == "none"
        # collapse happened, keeping only the target album
        monitor = next(c for c in calls if c[1] == "album/monitor")
        assert monitor[2]["json"] == {"albumIds": [3864, 3865], "monitored": False}

    async def test_collapse_converges_when_refresh_re_monitors(self):
        """The real-world race: RefreshArtist re-monitors after our PUT lands.

        Observed live on Ellie Goulding — one collapse un-monitored 5 siblings,
        then 3 came back monitored moments later.
        """
        state = {"3864": True, "3865": True}
        puts = {"count": 0}

        def handler(method, endpoint, **kwargs):
            if endpoint == "album" and method == "GET":
                return [
                    {"id": 3863, "monitored": True, "title": "Room on Fire"},
                    {"id": 3864, "monitored": state["3864"], "title": "Angles"},
                    {"id": 3865, "monitored": state["3865"], "title": "Is This It"},
                ]
            if endpoint == "album/monitor":
                puts["count"] += 1
                for aid in kwargs["json"]["albumIds"]:
                    state[str(aid)] = False
                # Simulate the background refresh re-monitoring one album.
                if puts["count"] == 1:
                    state["3865"] = True
                return {"ok": True}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, _ = make_service(handler)
        unset = await service.collapse_artist_monitoring(474, 3863)

        assert puts["count"] == 2, "must re-apply after the race"
        assert sorted(unset) == [3864, 3865]
        assert state == {"3864": False, "3865": False}

    async def test_collapse_reports_when_it_cannot_converge(self, no_real_sleep):
        """Non-convergence must be loud, not silently reported as success."""
        def handler(method, endpoint, **kwargs):
            if endpoint == "album" and method == "GET":
                # 3865 never turns off.
                return [
                    {"id": 3863, "monitored": True, "title": "Keep"},
                    {"id": 3865, "monitored": True, "title": "Stubborn"},
                ]
            if endpoint == "album/monitor":
                return {"ok": True}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, _ = make_service(handler)
        unset = await service.collapse_artist_monitoring(
            474, 3863, attempts=3, delay=0.01)

        assert unset == [3865]
        assert len(no_real_sleep) == 2  # slept between the 3 attempts, not after the last

    async def test_already_present_still_monitors_the_album(self):
        """An album already in Lidarr is not necessarily *wanted*.

        It may have been created unmonitored by an earlier artist refresh, so
        "Add" must still monitor it and collapse the artist — otherwise the
        button silently does nothing.
        """
        monitored = {1000: True, 999: False}

        def handler(method, endpoint, **kwargs):
            if endpoint in ("qualityprofile", "metadataprofile"):
                return [{"id": 1}]
            if endpoint == "rootfolder":
                return [{"path": "/data/music"}]
            if endpoint == "album/lookup":
                return LOOKUP_RESULT
            if endpoint.startswith("artist/"):
                return {"id": 1, "artistName": "The Strokes",
                        "monitorNewItems": "all", "monitored": False}
            if endpoint == "album" and method == "GET":
                if "foreignAlbumId" in kwargs.get("params", {}):
                    return [{"id": 999, "artistId": 1, "title": "Room on Fire",
                             "artist": {"artistName": "The Strokes"}}]
                return [
                    {"id": 999, "monitored": monitored[999], "title": "Room on Fire"},
                    {"id": 1000, "monitored": monitored[1000], "title": "Angles"},
                ]
            if endpoint == "album/monitor":
                for aid in kwargs["json"]["albumIds"]:
                    monitored[aid] = kwargs["json"]["monitored"]
                return {"ok": True}
            raise AssertionError(f"unexpected call {method} {endpoint}")

        service, calls = make_service(handler)
        result = await service.add_single_album("The Strokes", "Room on Fire")

        assert result["already_present"] is True
        assert result["album_id"] == 999
        # no duplicate album created
        assert not any(c[0] == "POST" and c[1] == "album" for c in calls)
        # but the album IS now monitored, and the sibling is not
        assert monitored == {999: True, 1000: False}
        assert result["monitoring_ok"] is True

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


class TestLookupAlbumFor:
    """Two-step lookup: combined term, then album name alone."""

    async def test_falls_back_to_album_name_when_combined_misses(self):
        """The live Blur case: "Blur Blur" never returns the 1997 album."""
        combined = [
            {"title": "Essential Blur", "foreignAlbumId": "comp",
             "artist": {"artistName": "Blur"}},
            {"title": "Massive Blur", "foreignAlbumId": "other",
             "artist": {"artistName": "Melissa Ferrick"}},
        ]
        album_only = [
            {"title": "Blur: The Best Of", "foreignAlbumId": "bestof",
             "artist": {"artistName": "Blur"}},
            {"title": "Blur", "foreignAlbumId": "studio",
             "artist": {"artistName": "Blur"}},
        ]
        service = LidarrService("http://x", "k")
        seen = []

        async def fake_lookup(term):
            seen.append(term)
            return combined if term == "Blur Blur" else album_only

        service.lookup_album = fake_lookup  # type: ignore[assignment]
        rows = await service.lookup_album_for("Blur", "Blur")

        assert seen == ["Blur Blur", "Blur"]
        assert rows[0]["foreignAlbumId"] == "studio"

    async def test_first_query_wins_when_exact_present(self):
        service = LidarrService("http://x", "k")
        seen = []

        async def fake_lookup(term):
            seen.append(term)
            return [{"title": "Halcyon", "foreignAlbumId": "studio",
                     "artist": {"artistName": "Ellie Goulding"}}]

        service.lookup_album = fake_lookup  # type: ignore[assignment]
        rows = await service.lookup_album_for("Ellie Goulding", "Halcyon")

        assert seen == ["Ellie Goulding Halcyon"]  # no second call needed
        assert rows[0]["foreignAlbumId"] == "studio"

    async def test_never_settles_for_other_artist_when_artist_known(self):
        service = LidarrService("http://x", "k")

        async def fake_lookup(term):
            return [
                {"title": "Halcyon", "foreignAlbumId": "impostor",
                 "artist": {"artistName": "Sevenism"}},
                {"title": "Lights", "foreignAlbumId": "real",
                 "artist": {"artistName": "Ellie Goulding"}},
            ]

        service.lookup_album = fake_lookup  # type: ignore[assignment]
        rows = await service.lookup_album_for("Ellie Goulding", "Halcyon")
        assert service._row_artist(rows[0]) == "Ellie Goulding"

    async def test_handles_empty_results(self):
        service = LidarrService("http://x", "k")

        async def fake_lookup(term):
            return []

        service.lookup_album = fake_lookup  # type: ignore[assignment]
        assert await service.lookup_album_for("X", "Y") == []
