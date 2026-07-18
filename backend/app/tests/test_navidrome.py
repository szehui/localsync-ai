"""Tests for Navidrome Subsonic API client."""
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from app.services.navidrome import NavidromeClient


@pytest.fixture
def client():
    return NavidromeClient(
        url="http://192.168.4.205:4533",
        username="testuser",
        password="testpass",
    )


def test_auth_params(client):
    """Auth params should include salt, token, username, and API version."""
    params = client._auth_params()
    assert params["u"] == "testuser"
    assert params["v"] == "1.16.1"
    assert params["c"] == "localsync-ai"
    assert params["f"] == "json"
    assert "t" in params  # token
    assert "s" in params  # salt
    assert len(params["s"]) == 32  # 16 bytes hex = 32 chars


def test_auth_token_is_valid_md5(client):
    """Token should be MD5 of password + salt."""
    import hashlib
    params = client._auth_params()
    expected = hashlib.md5(f"testpass{params['s']}".encode()).hexdigest()
    assert params["t"] == expected


@pytest.mark.asyncio
async def test_ping_success(client):
    """Ping should return server version on success."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
                "version": "0.53.3",
            }
        },
        request=httpx.Request("GET", "http://test/rest/ping.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        result = await client.ping()
        assert result["version"] == "0.53.3"


@pytest.mark.asyncio
async def test_ping_failure(client):
    """Ping should raise on Subsonic error response."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 40, "message": "Wrong username or password"},
            }
        },
        request=httpx.Request("GET", "http://test/rest/ping.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        with pytest.raises(Exception, match="Wrong username or password"):
            await client.ping()


@pytest.mark.asyncio
async def test_get_artists(client):
    """get_artists should flatten index structure into list."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
                "artists": {
                    "index": [
                        {
                            "name": "A",
                            "artist": [
                                {"id": "1", "name": "Artist A", "albumCount": 3},
                                {"id": "2", "name": "Artist B", "albumCount": 1},
                            ],
                        },
                        {
                            "name": "B",
                            "artist": [
                                {"id": "3", "name": "Artist C", "albumCount": 5},
                            ],
                        },
                    ]
                },
            }
        },
        request=httpx.Request("GET", "http://test/rest/getArtists.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        artists = await client.get_artists()
        assert len(artists) == 3
        assert artists[0]["name"] == "Artist A"
        assert artists[2]["name"] == "Artist C"


@pytest.mark.asyncio
async def test_get_similar_songs2(client):
    """get_similar_songs2 should return list of similar tracks."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
                "similarSongs2": {
                    "song": [
                        {"id": "s1", "title": "Similar Song 1", "artist": "Artist A"},
                        {"id": "s2", "title": "Similar Song 2", "artist": "Artist B"},
                    ]
                },
            }
        },
        request=httpx.Request("GET", "http://test/rest/getSimilarSongs2.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        songs = await client.get_similar_songs2("seed123", count=10)
        assert len(songs) == 2
        assert songs[0]["title"] == "Similar Song 1"


@pytest.mark.asyncio
async def test_create_playlist(client):
    """create_playlist should send songId params."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
                "playlist": {"id": "pl-123"},
            }
        },
        request=httpx.Request("GET", "http://test/rest/createPlaylist.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        result = await client.create_playlist("My Playlist", ["t1", "t2", "t3"])
        assert result["playlist"]["id"] == "pl-123"
        # Verify songId params were sent
        call_params = mock_get.call_args[1]["params"]
        assert call_params["songId"] == ["t1", "t2", "t3"]


@pytest.mark.asyncio
async def test_update_playlist(client):
    """update_playlist should include playlistId and songIds."""
    mock_response = httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
            }
        },
        request=httpx.Request("GET", "http://test/rest/updatePlaylist.view"),
    )
    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        await client.update_playlist("pl-456", ["t1", "t2"], name="Updated")
        call_params = mock_get.call_args[1]["params"]
        assert call_params["playlistId"] == "pl-456"
        assert call_params["name"] == "Updated"
        assert call_params["songId"] == ["t1", "t2"]
