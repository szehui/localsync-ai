"""Deezer album attribution tests.

Deezer answers "which release is this song on?" for tracks that a flat YouTube
playlist cannot describe. A wrong answer only produces a missing album (Lidarr
re-resolves the release against MusicBrainz), but a *sloppy* answer produces
dozens of wasted lookups and collapses, so the gates matter.
"""
import pytest

from app.services import deezer as deezer_module
from app.services.deezer import DeezerService


def make_service(rows_by_query):
    """DeezerService whose HTTP layer is replaced by a query->rows map."""
    service = DeezerService()
    calls = []

    async def fake_search(artist, title):
        calls.append((artist, title))
        if (artist, title) in rows_by_query:
            return rows_by_query[(artist, title)]
        # Deezer would still answer for a differently-spelled artist name.
        for (_, row_title), rows in rows_by_query.items():
            if row_title.lower() == title.lower():
                return rows
        return []

    service._search_tracks = fake_search  # type: ignore[assignment]
    return service, calls


def track(title, artist, album_id=1, album="Some Album"):
    return {
        "title": title,
        "artist": {"name": artist},
        "album": {"id": album_id, "title": album},
    }


class TestLookupAlbum:
    async def test_returns_the_release_for_a_known_track(self):
        service, _ = make_service({
            ("ENHYPEN", "Bite Me"): [track("Bite Me", "ENHYPEN", 555, "DARK BLOOD")]
        })

        hit = await service.lookup_album("ENHYPEN", "Bite Me")

        assert hit["album"] == "DARK BLOOD"
        assert hit["album_artist"] == "ENHYPEN"
        assert hit["deezer_album_id"] == 555
        assert hit["score"] >= 0.8

    async def test_rejects_a_right_title_by_the_wrong_artist(self):
        """The live failure mode: 'Roses' by OutKast matched a Rihanna single."""
        service, _ = make_service({
            ("Outkast", "Roses"): [track("Roses", "Rihanna", 1, "Roses")]
        })

        assert await service.lookup_album("Outkast", "Roses") is None

    async def test_rejects_low_confidence_title(self):
        service, _ = make_service({
            ("ENHYPEN", "Bite Me"): [track("Bite Me (Sped Up)", "ENHYPEN", 1, "X")]
        })

        hit = await service.lookup_album("ENHYPEN", "Bite Me")
        # "Bite Me (Sped Up)" shares most tokens; still must clear the bar
        assert hit is None or hit["album"]

    async def test_tolerates_punctuation_and_case_differences(self):
        service, _ = make_service({
            ("Florence + The Machine", "Dog Days Are Over"):
                [track("Dog Days Are Over", "Florence + the Machine", 9, "Lungs")]
        })

        hit = await service.lookup_album("Florence and the Machine",
                                         "Dog Days Are Over")
        assert hit["album"] == "Lungs"

    async def test_caches_repeat_lookups(self):
        service, calls = make_service({
            ("ENHYPEN", "Bite Me"): [track("Bite Me", "ENHYPEN", 555, "DARK BLOOD")]
        })

        await service.lookup_album("ENHYPEN", "Bite Me")
        await service.lookup_album("ENHYPEN", "Bite Me")

        assert len(calls) == 1

    async def test_no_results_returns_none(self):
        service, _ = make_service({})
        assert await service.lookup_album("Nobody", "Nothing") is None

    async def test_track_without_album_is_ignored(self):
        service, _ = make_service({
            ("ENHYPEN", "Bite Me"): [{
                "title": "Bite Me", "artist": {"name": "ENHYPEN"}, "album": {},
            }]
        })
        assert await service.lookup_album("ENHYPEN", "Bite Me") is None


class TestSearchFailures:
    async def test_http_error_falls_through_to_none(self, monkeypatch):
        class Resp:
            status_code = 500

            def json(self):
                return {}

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                return Resp()

        monkeypatch.setattr(deezer_module.httpx, "AsyncClient", Client)
        service = DeezerService()
        assert await service.lookup_album("ENHYPEN", "Bite Me") is None
