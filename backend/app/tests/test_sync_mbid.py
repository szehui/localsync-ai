"""Tests for track building with recording_mbid and search_key."""
import pytest
from app.services.sync import _build_track


def _make_song(**overrides):
    base = {
        "id": "t1", "title": "Track 1", "parent": "al1", "artistId": "a1",
        "artist": "Artist One", "genre": "Rock", "year": 2024,
        "duration": 180, "track": 1, "discNumber": 1,
        "playCount": 5, "rating": 4, "starred": False,
        "created": "2024-01-15T10:00:00Z",
    }
    base.update(overrides)
    return base


class TestBuildTrack:
    def test_records_musicbrainz_id(self):
        song = _make_song(musicBrainzId="30d08f4c-d825-4ae1-b79c-44242cddd7c0")
        track = _build_track(song, {})
        assert track.recording_mbid == "30d08f4c-d825-4ae1-b79c-44242cddd7c0"

    def test_none_when_no_musicbrainz_id(self):
        song = _make_song()
        song.pop("musicBrainzId", None)
        track = _build_track(song, {})
        assert track.recording_mbid is None

    def test_search_key_normalizes(self):
        song = _make_song(artist="Röyksopp", title="Some Resolve")
        track = _build_track(song, {})
        assert track.search_key == "royksopp|someresolve"

    def test_search_key_handles_missing_artist(self):
        song = _make_song(artist=None)
        track = _build_track(song, {})
        assert track.search_key == "|track1" or track.search_key == "|track1"

    def test_search_key_handles_missing_title(self):
        song = _make_song(title=None)
        track = _build_track(song, {})
        assert track.search_key == "artistone|"

    def test_search_key_is_deterministic_across_calls(self):
        song_a = _make_song(artist="The Beatles", title="Hey Jude")
        song_b = _make_song(artist="the beatles", title="HEY JUDE")
        track_a = _build_track(song_a, {})
        track_b = _build_track(song_b, {})
        assert track_a.search_key == track_b.search_key == "thebeatles|heyjude"

    def test_search_key_ignores_punctuation_and_spacing(self):
        variants = [
            ("AC/DC", "Back In Black"),
            ("ac dc", "back in black"),
            ("AC-DC", "Back  In  Black"),
        ]
        keys = {_build_track(_make_song(artist=a, title=t), {}).search_key
                for a, t in variants}
        assert keys == {"acdc|backinblack"}

