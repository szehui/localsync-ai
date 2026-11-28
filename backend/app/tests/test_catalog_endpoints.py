"""Tests for the catalog endpoints: auth, input validation, error mapping."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import get_db
from app.routers.auth import get_current_user
from app.services.musicbrainz import MusicBrainzUnavailable


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth():
    """Bypass JWT verification; the tenant shape is what the service expects."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "sze", "navidrome_url": "http://localhost:4535"}
    app.dependency_overrides[get_db] = lambda: object()
    yield


class TestAuth:
    @pytest.mark.parametrize("path", [
        "/api/catalog/search?q=hysteria",
        "/api/catalog/rails",
        "/api/catalog/album/aaaaaaaa-0000-0000-0000-000000000001",
        "/api/catalog/artist/aaaaaaaa-0000-0000-0000-000000000001",
    ])
    def test_requires_token(self, client, path):
        assert client.get(path).status_code == 401


class TestSearchValidation:
    def test_empty_query_is_400(self, client, auth):
        assert client.get("/api/catalog/search?q=", headers={}).status_code == 400

    def test_whitespace_query_is_400(self, client, auth):
        assert client.get("/api/catalog/search?q=%20%20").status_code == 400

    def test_empty_query_does_not_call_upstream(self, client, auth):
        with patch("app.services.catalog.search", new=AsyncMock()) as mock_search:
            assert client.get("/api/catalog/search?q=").status_code == 400
            mock_search.assert_not_awaited()

    def test_limit_bounds(self, client, auth):
        assert client.get("/api/catalog/search?q=x&limit=0").status_code == 422
        assert client.get("/api/catalog/search?q=x&limit=99").status_code == 422


class TestHappyPath:
    def test_search_returns_service_payload(self, client, auth):
        payload = {"results": [{"title": "Hysteria"}], "count": 1, "offset": 0}
        with patch("app.services.catalog.search", new=AsyncMock(return_value=payload)):
            response = client.get("/api/catalog/search?q=hysteria")
        assert response.status_code == 200
        assert response.json()["results"][0]["title"] == "Hysteria"

    def test_rails_returns_both_lists(self, client, auth):
        payload = {"new_releases": [], "trending": []}
        with patch("app.services.catalog.rails", new=AsyncMock(return_value=payload)):
            response = client.get("/api/catalog/rails")
        assert response.status_code == 200
        assert set(response.json()) == {"new_releases", "trending"}

    def test_album_404_when_missing(self, client, auth):
        with patch("app.services.catalog.album_detail", new=AsyncMock(return_value=None)):
            assert client.get("/api/catalog/album/nope").status_code == 404

    def test_artist_404_when_missing(self, client, auth):
        with patch("app.services.catalog.artist_detail", new=AsyncMock(return_value=None)):
            assert client.get("/api/catalog/artist/nope").status_code == 404


class TestUpstreamFailure:
    @pytest.mark.parametrize("path,patch_target", [
        ("/api/catalog/search?q=hysteria", "app.services.catalog.search"),
        ("/api/catalog/rails", "app.services.catalog.rails"),
        ("/api/catalog/album/x", "app.services.catalog.album_detail"),
        ("/api/catalog/artist/x", "app.services.catalog.artist_detail"),
    ])
    def test_musicbrainz_unavailable_is_503(self, client, auth, path, patch_target):
        with patch(patch_target, new=AsyncMock(side_effect=MusicBrainzUnavailable())):
            response = client.get(path)
        assert response.status_code == 503
        assert "busy" in response.json()["detail"]
