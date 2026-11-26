"""Batch album adds must not un-monitor each other.

Regression: adding eight ENHYPEN albums with `add_single_album` in a loop left
only the eighth monitored (34 of 88 albums in one live run), because each add
collapses the artist to the one album it just created.
"""
import pytest

from app.services import lidarr as lidarr_module
from app.services.lidarr import LidarrService


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(lidarr_module.asyncio, "sleep", fake_sleep)


class FakeLidarr:
    """Minimal in-memory Lidarr: artists own albums, albums own monitoring."""

    def __init__(self, lookup):
        self.lookup = lookup            # (artist, album) -> album row | None
        self.albums = {}                # album_id -> {artistId, monitored, title}
        self.artists = {}               # artist_id -> {monitorNewItems}
        self.calls = []
        self._next_album_id = 100
        self._next_artist_id = 1
        self.artist_ids = {}            # artist name -> id
        self.search_failures = set()

    def handler(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))

        if endpoint == "qualityprofile":
            return [{"id": 1}]
        if endpoint == "metadataprofile":
            return [{"id": 1}]
        if endpoint == "rootfolder":
            return [{"path": "/data/music"}]
        if endpoint == "album/lookup":
            return []  # never used: lookup_album_for is stubbed
        if endpoint == "album" and method == "GET" and "foreignAlbumId" in kwargs.get("params", {}):
            rg = kwargs["params"]["foreignAlbumId"]
            for aid, alb in self.albums.items():
                if alb["rg"] == rg:
                    return [{"id": aid, **alb}]
            return []
        if endpoint == "album" and method == "POST":
            payload = kwargs["json"]
            artist_name = payload["artist"]["artistName"]
            artist_id = self.artist_ids.setdefault(
                artist_name, self._alloc_artist())
            self.artists.setdefault(artist_id, {
                "monitorNewItems": payload["artist"]["monitorNewItems"]})
            album_id = self._next_album_id
            self._next_album_id += 1
            self.albums[album_id] = {
                "rg": payload["foreignAlbumId"],
                "artistId": artist_id,
                "monitored": payload["monitored"],
                "title": payload["title"],
            }
            return {"id": album_id, "artistId": artist_id,
                    "artist": {"artistName": artist_name}}
        if endpoint == "album" and method == "GET":
            artist_id = kwargs["params"]["artistId"]
            return [
                {"id": aid, "monitored": a["monitored"], "title": a["title"]}
                for aid, a in self.albums.items() if a["artistId"] == artist_id
            ]
        if endpoint == "album/monitor":
            for aid in kwargs["json"]["albumIds"]:
                self.albums[aid]["monitored"] = kwargs["json"]["monitored"]
            return {}
        if endpoint.startswith("artist/"):
            artist_id = int(endpoint.split("/")[1])
            return {"id": artist_id, **self.artists.setdefault(
                artist_id, {"monitorNewItems": "all"})}
        if endpoint.startswith("artist/") and method == "PUT":
            return {}
        if endpoint == "command":
            return {"id": 1}
        raise AssertionError(f"unexpected call {method} {endpoint}")

    def _alloc_artist(self):
        artist_id = self._next_artist_id
        self._next_artist_id += 1
        return artist_id

    def put_artist(self, method, endpoint, **kwargs):
        if method == "PUT" and endpoint.startswith("artist/"):
            artist_id = int(endpoint.split("/")[1])
            self.artists[artist_id] = kwargs["json"]
            return {}
        return self.handler(method, endpoint, **kwargs)

    def make_service(self):
        service = LidarrService("http://lidarr.test", "key")
        fake = self

        async def fake_request(method, endpoint, **kwargs):
            if method == "PUT" and endpoint.startswith("artist/"):
                return fake.put_artist(method, endpoint, **kwargs)
            return fake.handler(method, endpoint, **kwargs)

        async def fake_lookup_for(artist_name, release_name, **kwargs):
            row = fake.lookup.get((artist_name, release_name))
            return [row] if row else []

        async def fake_defaults():
            return {"quality_profile_id": 1, "metadata_profile_id": 1,
                    "root_folder": "/data/music"}

        service._request = fake_request          # type: ignore[assignment]
        service.lookup_album_for = fake_lookup_for  # type: ignore[assignment]
        service.get_album_defaults = fake_defaults  # type: ignore[assignment]
        return service


def album_row(title, artist, rg):
    return {"title": title, "foreignAlbumId": rg,
            "artist": {"artistName": artist, "foreignArtistId": f"{artist}-mbid"},
            "releases": [{"title": "CD"}]}


@pytest.fixture
def enhypen():
    fake = FakeLidarr({
        ("ENHYPEN", "DARK BLOOD"): album_row("DARK BLOOD", "ENHYPEN", "rg-dark-blood"),
        ("ENHYPEN", "ORANGE BLOOD"): album_row("ORANGE BLOOD", "ENHYPEN", "rg-orange-blood"),
        ("ENHYPEN", "ROMANCE : UNTOLD"): album_row("ROMANCE : UNTOLD", "ENHYPEN", "rg-romance"),
    })
    return fake


class TestAddAlbums:
    async def test_every_album_stays_monitored(self, enhypen):
        """The regression: three adds, three albums monitored — not just the last."""
        service = enhypen.make_service()
        items = [("ENHYPEN", "DARK BLOOD"),
                 ("ENHYPEN", "ORANGE BLOOD"),
                 ("ENHYPEN", "ROMANCE : UNTOLD")]

        outcome = await service.add_albums(items)

        assert len(outcome["added"]) == 3
        assert outcome["already_present"] == []
        assert outcome["failed"] == []
        assert outcome["monitoring_ok"] is True

        artist_id = enhypen.artist_ids["ENHYPEN"]
        monitored = [aid for aid, a in enhypen.albums.items()
                     if a["artistId"] == artist_id and a["monitored"]]
        assert len(monitored) == 3
        # monitorNewItems must be pinned so RefreshArtist cannot re-monitor
        assert enhypen.artists[artist_id]["monitorNewItems"] == "none"

    async def test_one_collapse_per_artist_not_per_album(self, enhypen):
        service = enhypen.make_service()
        await service.add_albums([
            ("ENHYPEN", "DARK BLOOD"),
            ("ENHYPEN", "ORANGE BLOOD"),
        ])

        positive = [c for c in enhypen.calls
                    if c[1] == "album/monitor" and c[2]["json"]["monitored"] is True]
        assert len(positive) == 1
        assert len(positive[0][2]["json"]["albumIds"]) == 2

    async def test_sibling_albums_still_unmonitored(self, enhypen):
        """Keeping our albums must not resurrect the artist's other releases."""
        service = enhypen.make_service()
        artist_id = enhypen.artist_ids.setdefault("ENHYPEN", enhypen._alloc_artist())
        enhypen.artists[artist_id] = {"monitorNewItems": "none"}
        enhypen.albums[900] = {"rg": "rg-old", "artistId": artist_id,
                               "monitored": True, "title": "BORDER : CARNIVAL"}

        await service.add_albums([("ENHYPEN", "DARK BLOOD")])

        assert enhypen.albums[900]["monitored"] is False

    async def test_a_failed_album_does_not_kill_the_batch(self, enhypen):
        service = enhypen.make_service()
        outcome = await service.add_albums([
            ("ENHYPEN", "DARK BLOOD"),
            ("ENHYPEN", "NOT A REAL ALBUM"),
        ])

        assert len(outcome["added"]) == 1
        assert len(outcome["failed"]) == 1
        assert "NOT A REAL ALBUM" in outcome["failed"][0]["release_name"]

    async def test_already_present_album_is_reported_and_monitored(self, enhypen):
        service = enhypen.make_service()
        first = await service.add_albums([("ENHYPEN", "DARK BLOOD")])
        album_id = first["added"][0]["album_id"]
        enhypen.albums[album_id]["monitored"] = False

        second = await service.add_albums([("ENHYPEN", "DARK BLOOD")])

        assert second["already_present"][0]["album_id"] == album_id
        assert enhypen.albums[album_id]["monitored"] is True

    async def test_two_artists_are_tracked_separately(self):
        fake = FakeLidarr({
            ("ENHYPEN", "DARK BLOOD"): album_row("DARK BLOOD", "ENHYPEN", "rg-1"),
            ("TXT", "The Name Chapter: TEMPTATION"):
                album_row("The Name Chapter: TEMPTATION", "TXT", "rg-2"),
        })
        service = fake.make_service()

        outcome = await service.add_albums([
            ("ENHYPEN", "DARK BLOOD"),
            ("TXT", "The Name Chapter: TEMPTATION"),
        ])

        assert outcome["monitoring_ok"] is True
        for artist_id in fake.artist_ids.values():
            assert fake.artists[artist_id]["monitorNewItems"] == "none"
        assert all(a["monitored"] for a in fake.albums.values())
