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


# ---- Title / artist cleaning (YouTube noise) ----

def test_clean_track_title_strips_remaster():
    from app.services.youtube_music import clean_track_title

    assert clean_track_title("Hotel California (2013 Remaster)") == "Hotel California"
    assert clean_track_title("Hotel California [2023 Remaster]") == "Hotel California"
    assert clean_track_title("Walk This Way (7\" Single Version)") == "Walk This Way"
    assert clean_track_title("Separate Ways (Worlds Apart) [2023 Remaster]") == \
        "Separate Ways (Worlds Apart)"
    assert clean_track_title("Juke Box Hero (Foreigner 4 Stereo Remix)") == "Juke Box Hero"


def test_clean_track_title_keeps_meaningful_titles():
    from app.services.youtube_music import clean_track_title

    # A leading parenthetical is part of the title, not edition noise.
    assert clean_track_title("(Don't Fear) The Reaper") == "(Don't Fear) The Reaper"
    assert clean_track_title("Bohemian Rhapsody") == "Bohemian Rhapsody"


def test_clean_artist_name_strips_channel_noise():
    from app.services.youtube_music import clean_artist_name

    assert clean_artist_name("Queen Official") == "Queen"
    assert clean_artist_name("Whitesnake TV") == "Whitesnake"
    assert clean_artist_name("Thomas Dolby Official") == "Thomas Dolby"
    assert clean_artist_name("BTS - Topic") == "BTS"
    assert clean_artist_name("Survivor Band") == "Survivor"
    assert clean_artist_name("HouseOfPainTV") == "HouseOfPain"
    assert clean_artist_name("SirMixAlotChannel") == "SirMixAlot"


def test_clean_artist_name_preserves_real_names():
    from app.services.youtube_music import clean_artist_name

    # "The Band" is an artist, not a channel suffix.
    assert clean_artist_name("The Band") == "The Band"
    assert clean_artist_name("Aerosmith") == "Aerosmith"
    assert clean_artist_name("") == ""


# ---- Artist gate: a right title with the wrong artist is a wrong song ----

def test_artist_gate_rejects_wrong_artist():
    from app.routers.youtube_import import _artist_agrees

    # The false positives seen on a real playlist.
    assert not _artist_agrees("Kool & The Gang", "They Might Be Giants")
    assert not _artist_agrees("Kris Kross", "Rihanna")
    assert not _artist_agrees("Eve 6", "Sara Bareilles")


def test_artist_gate_accepts_correct_artist():
    from app.routers.youtube_import import _artist_agrees

    assert _artist_agrees("Queen", "Queen")
    assert _artist_agrees("Journey", "Journey")
    # Channel-cleaned forms must agree with the library's punctuation.
    assert _artist_agrees("TheCranberries", "The Cranberries")


def test_artist_gate_passes_unknown_artist():
    from app.routers.youtube_import import _artist_agrees

    # No artist to compare means a title match is all the evidence there is.
    assert _artist_agrees("", "Rihanna")


def test_cleaned_title_scores_against_remastered_library_copy():
    """A YT title with a remaster suffix must still match the clean library copy."""
    from app.routers.youtube_import import _match_score
    from app.services.youtube_music import clean_track_title

    yt = clean_track_title("Separate Ways (Worlds Apart) [2023 Remaster]")
    lib = clean_track_title("Separate Ways (Worlds Apart)")
    assert _match_score(yt, "journey", lib, "Journey") == 1.0

    yt2 = clean_track_title("Juke Box Hero")
    lib2 = clean_track_title("Juke Box Hero (Foreigner 4 Stereo Remix)")
    assert _match_score(yt2, "Foreigner", lib2, "Foreigner") == 1.0


def test_preference_prefers_studio_over_live():
    """Cleaning ties "Song" with "Song (Live...)"; the tie-break must pick studio.

    Raw string similarity is the WRONG signal here: "2001 Remaster" resembles
    "Live; 2010 Remaster" more than it resembles the bare title, so a raw-score
    tie-break would deliberately choose the live take.
    """
    from app.routers.youtube_import import _match_score, _preference_key
    from app.services.youtube_music import clean_track_title

    yt_title, yt_artist = "Hungry Like the Wolf (2001 Remaster)", "Duran Duran"
    studio = "Hungry Like the Wolf"
    live = "Hungry Like the Wolf (Live; 2010 Remaster)"

    clean_studio = clean_track_title(studio)
    clean_live = clean_track_title(live)
    assert clean_studio == clean_live == "Hungry Like the Wolf"  # the tie

    raw_studio = _match_score(yt_title, yt_artist, studio, "Duran Duran")
    raw_live = _match_score(yt_title, yt_artist, live, "Duran Duran")
    assert raw_live > raw_studio  # proves raw score is misleading

    pref_studio = _preference_key(studio, clean_studio, raw_studio)
    pref_live = _preference_key(live, clean_live, raw_live)
    assert pref_studio > pref_live


def test_preference_falls_back_to_raw_score():
    """When both copies carry noise, prefer the closer raw title."""
    from app.routers.youtube_import import _preference_key
    from app.services.youtube_music import clean_track_title

    yt = "Go Your Own Way (2004 Remaster)"
    same = "Go Your Own Way (2004 Remaster)"
    other = "Go Your Own Way (2001 Remaster)"

    clean_same = clean_track_title(same)
    clean_other = clean_track_title(other)
    # Neither needed no cleaning, so raw proximity decides.
    assert _preference_key(same, clean_same, 1.0) > _preference_key(other, clean_other, 0.8)
