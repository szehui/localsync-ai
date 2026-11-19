"""Tests for YouTube Music import feature."""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.youtube_music import YouTubeMusicService, _parse_artist_title
from app.services.lidarr import LidarrService
from app.routers.youtube_import import _normalize_text, _match_score, _tokenize


# ---- Text normalization / matching ----

def test_normalize():
    assert _normalize_text("Hello World!") == "hello world"
    assert _normalize_text("  spaces  ") == "spaces"
    assert _normalize_text("UPPER CASE") == "upper case"


def test_tokenize():
    tokens = _tokenize("The Florence and the Machine")
    assert "florence" in tokens
    assert "machine" in tokens
    assert "the" not in tokens
    assert "and" not in tokens


def test_match_score_perfect():
    assert _match_score("Bohemian Rhapsody", "Queen", "Bohemian Rhapsody", "Queen") == 1.0


def test_match_score_fuzzy_title():
    score = _match_score("Bohemian Rhapsody", "Queen", "Bohemian Rhapsod", "Queen")
    assert score > 0.9


def test_match_score_different():
    score = _match_score("Bohemian Rhapsody", "Queen", "Stairway to Heaven", "Led Zeppelin")
    assert score < 0.5


def test_match_score_artist_variation():
    """'Florence + The Machine' should match 'Florence and the Machine'."""
    score = _match_score(
        "Dog Days Are Over", "Florence and the Machine",
        "Dog Days Are Over", "Florence + The Machine",
    )
    assert score >= 0.75


def test_match_score_case_insensitive():
    score = _match_score("dog days are over", "florence", "Dog Days Are Over", "Florence")
    assert score >= 0.75


# ---- Artist/Title parsing ----

def test_parse_artist_title_standard_dash():
    artist, title = _parse_artist_title("Queen - Bohemian Rhapsody")
    assert artist == "Queen"
    assert title == "Bohemian Rhapsody"


def test_parse_artist_title_em_dash():
    artist, title = _parse_artist_title("Queen – Bohemian Rhapsody")
    assert artist == "Queen"
    assert title == "Bohemian Rhapsody"


def test_parse_artist_title_with_suffix():
    artist, title = _parse_artist_title("Queen - Bohemian Rhapsody (Official Video)")
    assert artist == "Queen"
    assert title == "Bohemian Rhapsody"


def test_parse_artist_title_no_artist():
    artist, title = _parse_artist_title("Bohemian Rhapsody")
    assert artist == ""
    assert title == "Bohemian Rhapsody"


def test_parse_artist_title_colon():
    artist, title = _parse_artist_title("Queen: Bohemian Rhapsody")
    assert artist == "Queen"
    assert title == "Bohemian Rhapsody"


# ---- URL extraction ----

def test_extract_playlist_id_music_url():
    svc = YouTubeMusicService()
    assert svc._extract_playlist_id(
        "https://music.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
    ) == "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def test_extract_playlist_id_youtube_url():
    svc = YouTubeMusicService()
    assert svc._extract_playlist_id(
        "https://www.youtube.com/playlist?list=PL1234567890"
    ) == "PL1234567890"


def test_extract_playlist_id_bare_id():
    svc = YouTubeMusicService()
    assert svc._extract_playlist_id("PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf") == \
        "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def test_extract_playlist_id_invalid_url():
    svc = YouTubeMusicService()
    with pytest.raises(ValueError):
        svc._extract_playlist_id("https://example.com/not-a-playlist")


# ---- yt-dlp integration ----

def test_get_playlist_tracks_ytdlp(monkeypatch):
    track1 = json.dumps({
        "title": "Queen - Bohemian Rhapsody",
        "id": "abc123", "channel": "Queen Official", "duration": 359,
        "playlist_title": "My Playlist", "playlist": "My Playlist",
        "playlist_count": 3, "playlist_index": 1,
    })
    track2 = json.dumps({
        "title": "Led Zeppelin - Stairway to Heaven",
        "id": "def456", "channel": "Led Zeppelin", "duration": 482,
        "playlist_title": "My Playlist", "playlist": "My Playlist",
        "playlist_count": 3, "playlist_index": 2,
    })
    track3 = json.dumps({
        "title": "Just A Song Title",
        "id": "ghi789", "channel": "Unknown Artist", "duration": 200,
        "playlist_title": "My Playlist", "playlist": "My Playlist",
        "playlist_count": 3, "playlist_index": 3,
    })
    mock_output = "\n".join([track1, track2, track3])

    def mock_run(cmd, capture_output, text, timeout):
        m = MagicMock()
        m.returncode = 0
        m.stdout = mock_output
        m.stderr = ""
        return m

    monkeypatch.setattr("subprocess.run", mock_run)

    svc = YouTubeMusicService()
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        svc.get_playlist_tracks("PLtest123")
    )

    assert result["title"] == "My Playlist"
    assert result["track_count"] == 3
    assert result["tracks"][0]["artist"] == "Queen"
    assert result["tracks"][0]["title"] == "Bohemian Rhapsody"
    assert result["tracks"][1]["artist"] == "Led Zeppelin"
    assert result["tracks"][1]["title"] == "Stairway to Heaven"
    assert result["tracks"][2]["artist"] == "Unknown Artist"


def test_get_playlist_tracks_empty(monkeypatch):
    def mock_run(cmd, capture_output, text, timeout):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr("subprocess.run", mock_run)

    svc = YouTubeMusicService()
    import asyncio
    with pytest.raises(ValueError, match="No data returned"):
        asyncio.get_event_loop().run_until_complete(
            svc.get_playlist_tracks("PLempty")
        )


def test_get_playlist_tracks_not_found(monkeypatch):
    def mock_run(cmd, capture_output, text, timeout):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "ERROR: [youtube:tab] xxx: Unable to download API page: HTTP Error 404: Not Found"
        return m

    monkeypatch.setattr("subprocess.run", mock_run)

    svc = YouTubeMusicService()
    import asyncio
    with pytest.raises(ValueError, match="Playlist not found"):
        asyncio.get_event_loop().run_until_complete(
            svc.get_playlist_tracks("PLnotfound")
        )


def test_get_playlist_tracks_title_from_entry(monkeypatch):
    entry = json.dumps({
        "title": "My Song",
        "id": "xyz123",
        "channel": "Artist Name",
        "duration": 180,
        "playlist_title": "Cool Mix 2024",
        "playlist": "Cool Mix 2024",
    })

    def mock_run(cmd, capture_output, text, timeout):
        m = MagicMock()
        m.returncode = 0
        m.stdout = entry
        m.stderr = ""
        return m

    monkeypatch.setattr("subprocess.run", mock_run)

    svc = YouTubeMusicService()
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        svc.get_playlist_tracks("PLtest")
    )

    assert result["title"] == "Cool Mix 2024"
    assert result["track_count"] == 1
    assert result["tracks"][0]["title"] == "My Song"


# ---- Lidarr service tests ----

def test_lidarr_init():
    svc = LidarrService("http://localhost:8686", "test-key")
    assert svc.base_url == "http://localhost:8686"
    assert svc.api_key == "test-key"


def test_lidarr_headers():
    svc = LidarrService("http://localhost:8686", "my-api-key")
    assert svc.headers["X-Api-Key"] == "my-api-key"
