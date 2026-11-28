"""Tests for the composed home rails.

`/catalog/rails` returned 500 in production while every unit test passed,
because the only rails tests mocked the service out at the router boundary and
the parsing tests used fixtures that did not match the live payload. These tests
exercise the composition itself: the cap, the artwork budget, and what
"trending" actually ranks by.
"""
from unittest.mock import AsyncMock, patch

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.services import catalog


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _fresh_row(i: int, *, cover: bool = True) -> dict:
    return {
        "release_group_mbid": f"rg-{i}",
        "title": f"Album {i}",
        "artist_name": f"Artist {i}",
        "artist_mbid": f"artist-{i}",
        "year": 2026,
        "primary_type": "Album",
        "secondary_types": [],
        "cover_url": f"https://coverartarchive.org/release/rel-{i}/front-250"
        if cover else None,
        "score": 0,
        # Row 0 is the newest, so "newest first" is checkable rather than a
        # coincidence of the fixture.
        "release_date": (dt.date(2026, 11, 27) - dt.timedelta(days=i)).isoformat(),
    }


class TestRailsComposition:
    async def test_caps_the_rail_instead_of_returning_everything(self, db):
        rows = [_fresh_row(i) for i in range(200)]
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})):
            out = await catalog.rails(db, {"id": 1})
        assert len(out["new_releases"]) == catalog.RAIL_LIMIT
        assert out["new_releases"][0]["release_group_mbid"] == "rg-0"
        assert out["new_releases"][-1]["release_group_mbid"] == f"rg-{catalog.RAIL_LIMIT - 1}"

    async def test_popularity_is_asked_about_release_groups_not_artists(self, db):
        rows = [_fresh_row(i) for i in range(5)]
        pop = AsyncMock(return_value={"rg-3": 50, "rg-1": 10})
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", new=pop):
            out = await catalog.rails(db, {"id": 1})
        requested = pop.await_args.args[0]
        assert "rg-3" in requested
        assert all(mbid.startswith("rg-") for mbid in requested)
        # Ranked by release-group listens: the whole point of the rail.
        assert out["trending"][0]["release_group_mbid"] == "rg-3"
        assert [c["release_group_mbid"] for c in out["trending"]] == ["rg-3", "rg-1"]

    async def test_supplied_covers_avoid_a_musicbrainz_lookup(self, db):
        rows = [_fresh_row(i) for i in range(3)]
        covers = AsyncMock(return_value={})
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})), \
             patch("app.services.catalog._covers", new=covers):
            out = await catalog.rails(db, {"id": 1})
        covers.assert_not_awaited()
        assert out["new_releases"][0]["cover_url"].endswith("/front-250")

    async def test_missing_covers_fall_back_to_a_bounded_lookup(self, db):
        rows = [_fresh_row(i, cover=False) for i in range(3)]
        covers = AsyncMock(return_value={"rg-0": "https://example/0.jpg"})
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})), \
             patch("app.services.catalog._covers", new=covers):
            out = await catalog.rails(db, {"id": 1})
        covers.assert_awaited_once()
        assert out["new_releases"][0]["cover_url"] == "https://example/0.jpg"

    async def test_trending_drops_albums_with_no_listens(self, db):
        rows = [_fresh_row(i) for i in range(3)]
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={"rg-1": 5})):
            out = await catalog.rails(db, {"id": 1})
        assert [c["release_group_mbid"] for c in out["trending"]] == ["rg-1"]
