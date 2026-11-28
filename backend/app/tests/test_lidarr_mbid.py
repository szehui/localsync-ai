"""The MBID add path: exact resolution, one collapse per artist.

This is the path a request approval takes. Two failure modes it exists to
prevent, both observed before the MBID path existed:

* a fuzzy title lookup picking a *different* album by the same artist and
  queueing it under the right artist's name (Shang-Chi resolved to Savage Mode);
* collapsing monitoring per add, which un-monitors the previous add
  (34 of 88 albums lost in a live run).

So these tests assert identity, not just "an add happened": the album posted
must carry the requested release group, and an unresolved MBID must be reported
rather than replaced by a nearby result.
"""
import pytest

from app.services import lidarr as lidarr_module
from app.services.lidarr import LidarrService


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(lidarr_module.asyncio, "sleep", fake_sleep)
    return delays


class FakeLidarr:
    """Lidarr with an MBID-keyed catalogue and real monitoring semantics."""

    def __init__(self, catalogue):
        # rg mbid -> lookup row
        self.catalogue = catalogue
        self.albums = {}      # album_id -> {rg, artistId, monitored, title}
        self.artists = {}     # artist_id -> {monitorNewItems}
        self.artist_ids = {}
        self.calls = []
        self.monitor_puts = []
        self.lookups = []
        self._next_album_id = 100
        self._next_artist_id = 1

    def _alloc_artist(self):
        artist_id = self._next_artist_id
        self._next_artist_id += 1
        return artist_id

    def handler(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))

        if endpoint == "qualityprofile":
            return [{"id": 1}]
        if endpoint == "metadataprofile":
            return [{"id": 1}]
        if endpoint == "rootfolder":
            return [{"path": "/data/music"}]

        if endpoint == "album/lookup":
            term = kwargs["params"]["term"]
            self.lookups.append(term)
            assert term.startswith("mbid:"), (
                f"request path must look up by mbid, got {term!r}")
            mbid = term.split(":", 1)[1]
            row = self.catalogue.get(mbid)
            return [row] if row else []

        if endpoint == "album" and method == "GET" and "foreignAlbumId" in kwargs.get("params", {}):
            rg = kwargs["params"]["foreignAlbumId"]
            for aid, alb in self.albums.items():
                if alb["rg"] == rg:
                    return [{"id": aid, **alb}]
            return []

        if endpoint == "album" and method == "POST":
            payload = kwargs["json"]
            # The posted album must be the one that was resolved: identity is
            # carried by foreignAlbumId, never by title similarity.
            assert payload["foreignAlbumId"] in self.catalogue, payload
            artist_name = payload["artist"]["artistName"]
            artist_id = self.artist_ids.setdefault(artist_name, self._alloc_artist())
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
            return [{"id": aid, "monitored": a["monitored"], "title": a["title"]}
                    for aid, a in self.albums.items() if a["artistId"] == artist_id]

        if endpoint == "album/monitor":
            self.monitor_puts.append(kwargs["json"])
            for aid in kwargs["json"]["albumIds"]:
                self.albums[aid]["monitored"] = kwargs["json"]["monitored"]
            return {}

        if endpoint.startswith("artist/") and method == "GET":
            artist_id = int(endpoint.split("/")[1])
            return {"id": artist_id,
                    **self.artists.setdefault(artist_id, {"monitorNewItems": "all"})}

        if endpoint.startswith("artist/") and method == "PUT":
            artist_id = int(endpoint.split("/")[1])
            self.artists[artist_id] = kwargs["json"]
            return {}

        if endpoint == "command":
            return {"id": 1}

        raise AssertionError(f"unexpected call {method} {endpoint}")

    def make_service(self, combined_trigger=None):
        service = LidarrService("http://lidarr.test", "key")
        fake = self

        async def fake_request(method, endpoint, **kwargs):
            return fake.handler(method, endpoint, **kwargs)

        async def fake_defaults():
            return {"quality_profile_id": 1, "metadata_profile_id": 1,
                    "root_folder": "/data/music"}

        service._request = fake_request            # type: ignore[assignment]
        service.get_album_defaults = fake_defaults  # type: ignore[assignment]
        return service


def album_row(title, artist, rg, artist_mbid=None):
    return {
        "title": title,
        "foreignAlbumId": rg,
        "artist": {"artistName": artist,
                   "foreignArtistId": artist_mbid or f"{artist}-mbid"},
        "releases": [{"title": "CD"}],
    }


@pytest.fixture
def daft_punk():
    return FakeLidarr({
        "rg-ram": album_row("Random Access Memories", "Daft Punk", "rg-ram"),
        "rg-discovery": album_row("Discovery", "Daft Punk", "rg-discovery"),
        "rg-homework": album_row("Homework", "Daft Punk", "rg-homework"),
    })


class TestLookupByMbid:
    async def test_returns_the_row_with_the_requested_id(self, daft_punk):
        service = daft_punk.make_service()
        row = await service.lookup_album_by_mbid("rg-ram")
        assert row["foreignAlbumId"] == "rg-ram"

    async def test_a_mismatched_row_is_refused_not_used(self):
        """Lidarr answering with someone else's album must not be trusted.

        "One row comes back" is an observed property of one Lidarr version, not
        a contract, and adding the wrong album is not undoable from the user's
        side.
        """
        fake = FakeLidarr({
            # Asked for rg-ram, Lidarr answers with a *different* release group.
            "rg-ram": album_row("Random Access Memories", "Daft Punk", "rg-SOMETHING-ELSE"),
        })
        service = fake.make_service()

        assert await service.lookup_album_by_mbid("rg-ram") is None

    async def test_missing_mbid_returns_none_without_a_call(self, daft_punk):
        service = daft_punk.make_service()
        assert await service.lookup_album_by_mbid("") is None
        assert daft_punk.lookups == []


class TestAddAlbumsByMbid:
    async def test_two_albums_one_artist_get_one_monitor_put(self, daft_punk):
        """The whole point of the batch: a single monitoring update for the artist.

        A per-add collapse would issue one PUT per album, each un-monitoring the
        one before it. Asserting *exactly one* PUT is the strongest form of the
        regression guard: it means no per-add collapse ran at all, and both
        albums were turned on together.
        """
        service = daft_punk.make_service()
        outcome = await service.add_albums_by_mbid(["rg-ram", "rg-discovery"])

        assert len(outcome["added"]) == 2
        assert outcome["monitoring_ok"] is True

        assert len(daft_punk.monitor_puts) == 1, daft_punk.monitor_puts
        put = daft_punk.monitor_puts[0]
        assert put["monitored"] is True
        assert len(put["albumIds"]) == 2

        artist_id = daft_punk.artist_ids["Daft Punk"]
        still_monitored = {aid for aid, a in daft_punk.albums.items()
                           if a["artistId"] == artist_id and a["monitored"]}
        assert len(still_monitored) == 2, (
            "both requested albums must stay monitored")

    async def test_unresolved_mbid_is_reported_and_never_substituted(self, daft_punk):
        """A request for an album Lidarr cannot resolve fails visibly.

        Falling back to a title search here would download a different album
        while reporting success.
        """
        service = daft_punk.make_service()
        outcome = await service.add_albums_by_mbid(["rg-ram", "rg-does-not-exist"])

        assert len(outcome["added"]) == 1
        assert len(outcome["unresolved"]) == 1
        assert outcome["unresolved"][0]["release_group_mbid"] == "rg-does-not-exist"
        assert outcome["failed"] == []
        # Exactly one album was created, and only for the resolvable id.
        assert len(daft_punk.albums) == 1
        assert [rgs["rg"] for rgs in daft_punk.albums.values()] == ["rg-ram"]

    async def test_every_lookup_uses_the_mbid_form(self, daft_punk):
        """Guards against a refactor drifting back to the fuzzy title path.

        The FakeLidarr handler asserts on the term, but a regression could also
        skip the lookup entirely; pinning the calls makes that visible.
        """
        service = daft_punk.make_service()
        await service.add_albums_by_mbid(["rg-homework"])
        assert daft_punk.lookups == ["mbid:rg-homework"]

    async def test_already_present_album_is_still_monitored(self, daft_punk):
        """An album Lidarr knows but does not watch must become watched.

        `POST /album` creates the whole discography unmonitored, so "the row
        exists" is not "the request is satisfied".
        """
        artist_id = daft_punk.artist_ids.setdefault("Daft Punk", 1)
        daft_punk.artists[artist_id] = {"monitorNewItems": "none"}
        daft_punk.albums[77] = {"rg": "rg-ram", "artistId": artist_id,
                                "monitored": False, "title": "Random Access Memories"}

        service = daft_punk.make_service()
        outcome = await service.add_albums_by_mbid(["rg-ram"])

        assert len(outcome["already_present"]) == 1
        assert daft_punk.albums[77]["monitored"] is True

    async def test_empty_batch_makes_no_calls(self):
        fake = FakeLidarr({})
        service = fake.make_service()
        outcome = await service.add_albums_by_mbid([])
        assert outcome["added"] == []
        assert fake.calls == []

    async def test_transport_failure_is_distinct_from_unresolved(self, daft_punk):
        """A 503 is retryable; a missing release group is not.

        The reconciler and the retry endpoint need to tell them apart.
        """
        service = daft_punk.make_service()
        real_request = service._request

        async def flaky(method, endpoint, **kwargs):
            if method == "GET" and endpoint == "album/lookup" \
                    and kwargs["params"]["term"] == "mbid:rg-discovery":
                raise RuntimeError("Lidarr is busy")
            return await real_request(method, endpoint, **kwargs)

        service._request = flaky  # type: ignore[assignment]
        outcome = await service.add_albums_by_mbid(["rg-ram", "rg-discovery"])

        assert len(outcome["unresolved"]) == 0
        assert len(outcome["failed"]) == 1
        assert "busy" in outcome["failed"][0]["error"]
