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


class TestUnreleasedAlbums:
    """The rail says "last 14 days"; it must not be showing next month.

    ListenBrainz's `days` window is centred on today, so a days=14 request spans
    [today-14, today+14]. Sorting newest-first then made the pool entirely
    future-dated: live, all 24 cards were 2026-12-11/12-12 and no album released
    in the window appeared at all.
    """

    async def test_future_dated_rows_are_excluded(self, db):
        today = dt.datetime.now(dt.timezone.utc).date()
        rows = [_fresh_row(0)]
        for i, offset in enumerate((1, 7, 14), start=1):
            row = _fresh_row(i)
            row["release_date"] = (today + dt.timedelta(days=offset)).isoformat()
            rows.append(row)

        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=rows)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})):
            out = await catalog.rails(db, {"id": 1})

        ids = [c["release_group_mbid"] for c in out["new_releases"]]
        assert ids == ["rg-0"], ids

    async def test_a_pile_of_future_rows_cannot_crowd_out_released_ones(self, db):
        """The live failure: anticipation releases filled the 200-row pool."""
        today = dt.datetime.now(dt.timezone.utc).date()
        future = []
        for i in range(catalog.RAIL_POOL + 5):
            row = _fresh_row(i)
            row["release_date"] = (today + dt.timedelta(days=1 + i % 10)).isoformat()
            future.append(row)
        released = [_fresh_row(900)]
        released[0]["release_date"] = today.isoformat()

        with patch("app.services.lb_rails.fresh_releases",
                   AsyncMock(return_value=future + released)), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})):
            out = await catalog.rails(db, {"id": 1})

        ids = [c["release_group_mbid"] for c in out["new_releases"]]
        assert "rg-900" in ids, ids

    async def test_undated_rows_are_kept(self, db):
        """A row ListenBrainz did not date is still inside its own window."""
        row = _fresh_row(0)
        row.pop("release_date")
        with patch("app.services.lb_rails.fresh_releases", AsyncMock(return_value=[row])), \
             patch("app.services.lb_rails.popularity", AsyncMock(return_value={})):
            out = await catalog.rails(db, {"id": 1})
        assert [c["release_group_mbid"] for c in out["new_releases"]] == ["rg-0"]
