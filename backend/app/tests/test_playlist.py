"""Tests for playlist generation logic."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.routers.playlists import _passes_strictness


class TestStrictnessFilter:
    """Test the strictness-based track filtering."""

    def _make_track(self, artist_id="a1", genre="Rock", album_id="al1"):
        t = MagicMock()
        t.artist_id = artist_id
        t.genre = genre
        t.album_id = album_id
        return t

    def test_strictness_1_no_filter(self):
        """Strictness 1: no filtering, everything passes."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a5", "Classical")
        assert _passes_strictness(track, seed, 1) is True

    def test_strictness_2_same_artist(self):
        """Strictness 2: passes if same artist."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a1", "Classical")
        assert _passes_strictness(track, seed, 2) is True

    def test_strictness_2_same_album(self):
        """Strictness 2: passes if same album."""
        seed = self._make_track("a1", "Rock", "al1")
        track = self._make_track("a5", "Rock", "al1")
        assert _passes_strictness(track, seed, 2) is True

    def test_strictness_2_different(self):
        """Strictness 2: fails if different artist and album."""
        seed = self._make_track("a1", "Rock", "al1")
        track = self._make_track("a5", "Classical", "al99")
        assert _passes_strictness(track, seed, 2) is False

    def test_strictness_3_same_genre(self):
        """Strictness 3: passes if same genre."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a5", "Rock")
        assert _passes_strictness(track, seed, 3) is True

    def test_strictness_3_same_artist(self):
        """Strictness 3: passes if same artist."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a1", "Jazz")
        assert _passes_strictness(track, seed, 3) is True

    def test_strictness_4_same_genre_required(self):
        """Strictness 4: only same genre passes."""
        seed = self._make_track("a1", "Rock")
        track_diff_genre = self._make_track("a5", "Jazz")
        assert _passes_strictness(track_diff_genre, seed, 4) is False

    def test_strictness_4_same_genre(self):
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a5", "Rock")
        assert _passes_strictness(track, seed, 4) is True

    def test_strictness_5_same_artist_and_genre(self):
        """Strictness 5: requires BOTH same artist AND same genre."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a1", "Rock")
        assert _passes_strictness(track, seed, 5) is True

    def test_strictness_5_only_artist(self):
        """Strictness 5: fails if only artist matches."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a1", "Jazz")
        assert _passes_strictness(track, seed, 5) is False

    def test_strictness_5_only_genre(self):
        """Strictness 5: fails if only genre matches."""
        seed = self._make_track("a1", "Rock")
        track = self._make_track("a5", "Rock")
        assert _passes_strictness(track, seed, 5) is False

    def test_none_genre_handling(self):
        """None genre should not crash."""
        seed = self._make_track("a1", None)
        track = self._make_track("a5", "Rock")
        assert _passes_strictness(track, seed, 3) is False
