"""Routing tests for the recency trigger's data sources.

ListenBrainz is primary; Last.fm must only be contacted when ListenBrainz
produced nothing usable. These tests assert that routing directly, because the
whole point of the migration is to stop depending on Last.fm.
"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.database import (
    ConnectionConfig,
    GeneratedPlaylist,
    LastfmConfig,
    ListenbrainzConfig,
    SmartTrigger,
)
import app.services.scheduler as scheduler


@pytest.fixture
def trigger():
    t = MagicMock(spec=SmartTrigger)
    t.id = 1
    t.name = "Daily Mix"
    t.trigger_type = "recency"
    t.enabled = True
    t.playlist_name = None
    t.last_run = None
    t.threshold = 5
    t.owner_id = 1
    return t


def _make_db(trigger, *, lb_configured: bool, lastfm_configured: bool):
    def fake_query(model, *columns):
        # SQLAlchemy accepts column lists (db.query(Track.id, Track.genre));
        # the fake must too or the metadata lookup raises TypeError.
        m = MagicMock()
        if model is SmartTrigger:
            m.filter.return_value.first.return_value = trigger
        elif model is ListenbrainzConfig:
            m.filter.return_value.first.return_value = (
                MagicMock(username="szeis4cookie", user_token="tok") if lb_configured else None
            )
        elif model is LastfmConfig:
            m.filter.return_value.first.return_value = (
                MagicMock(api_key="k", username="u") if lastfm_configured else None
            )
        elif model is ConnectionConfig:
            m.filter.return_value.first.return_value = MagicMock(
                url="http://nav", username="u", password="p"
            )
        elif model is GeneratedPlaylist:
            m.filter.return_value.first.return_value = None
            m.filter.return_value.filter.return_value.all.return_value = []
        else:
            m.filter.return_value.first.return_value = None
            m.filter.return_value.filter.return_value.all.return_value = []
        return m

    db = MagicMock()
    db.query.side_effect = fake_query
    return db


def _make_client(pushed):
    client = MagicMock()
    client.close = AsyncMock()
    client.get_album_list2 = AsyncMock(return_value=[{"id": "a1"}])
    client.get_album = AsyncMock(return_value={"album": {"song": [
        {"id": f"new{i}", "artist_name": f"Artist {i}"} for i in range(25)
    ]}})
    client.delete_playlist = AsyncMock()
    client.search = AsyncMock(return_value={"searchResult3": {"song": []}})

    async def create_playlist(name, track_ids):
        pushed.append(list(track_ids))
        return {"playlist": {"id": "new-pid"}}

    client.create_playlist = create_playlist
    return client


class TestListenBrainzIsPrimary:
    @pytest.mark.asyncio
    async def test_lastfm_never_contacted_when_lb_covers(self, monkeypatch, trigger):
        pushed = []
        client = _make_client(pushed)
        db = _make_db(trigger, lb_configured=True, lastfm_configured=True)

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())
        monkeypatch.setattr(
            scheduler, "_gather_lb_candidates",
            AsyncMock(return_value=[{"artist": "", "title": "", "recording_mbid": "m1"}]),
        )
        monkeypatch.setattr(scheduler, "resolve_entries", lambda d, e: (["lb1", "lb2", "lb3"], []))

        built = []
        monkeypatch.setattr(
            scheduler, "LastfmClient",
            lambda *a, **k: built.append(1) or MagicMock(),
        )

        await scheduler.run_recency_trigger(1)

        assert built == [], "Last.fm was contacted even though ListenBrainz had data"
        assert pushed, "no playlist pushed"
        assert {"lb1", "lb2", "lb3"} & set(pushed[0])

    @pytest.mark.asyncio
    async def test_lb_fills_history_when_navidrome_similarity_is_empty(self, monkeypatch, trigger):
        pushed = []
        client = _make_client(pushed)
        # Navidrome similarity unavailable — ListenBrainz must cover all 30
        # history slots rather than shipping a short playlist.
        client.get_similar_songs2 = AsyncMock(return_value=[])
        db = _make_db(trigger, lb_configured=True, lastfm_configured=False)

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())
        monkeypatch.setattr(
            scheduler, "_gather_lb_candidates",
            AsyncMock(return_value=[{"artist": "x", "title": "y", "recording_mbid": "m"}]),
        )
        monkeypatch.setattr(
            scheduler, "resolve_entries",
            lambda d, e: ([f"lb{i}" for i in range(40)], []),
        )

        await scheduler.run_recency_trigger(1)

        assert pushed
        lb_in_output = sum(1 for t in pushed[0] if str(t).startswith("lb"))
        assert lb_in_output == 30, f"expected 30 LB tracks, got {lb_in_output}"
        assert len(pushed[0]) == 50

    @pytest.mark.asyncio
    async def test_lb_split_is_60_40_with_navidrome_similarity(self, monkeypatch, trigger):
        pushed = []
        client = _make_client(pushed)
        client.get_similar_songs2 = AsyncMock(
            return_value=[{"id": f"nav{i}"} for i in range(40)]
        )
        db = _make_db(trigger, lb_configured=True, lastfm_configured=False)

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())
        monkeypatch.setattr(
            scheduler, "_gather_lb_candidates",
            AsyncMock(return_value=[{"artist": "x", "title": "y", "recording_mbid": "m"}]),
        )
        monkeypatch.setattr(
            scheduler, "resolve_entries",
            lambda d, e: ([f"lb{i}" for i in range(40)], []),
        )

        await scheduler.run_recency_trigger(1)

        assert pushed
        lb_in_output = sum(1 for t in pushed[0] if str(t).startswith("lb"))
        nav_in_output = sum(1 for t in pushed[0] if str(t).startswith("nav"))
        # 60% of the 30 history slots from LB, 40% from Navidrome similarity.
        assert lb_in_output == 18, f"expected 18 LB tracks, got {lb_in_output}"
        assert nav_in_output == 12, f"expected 12 nav tracks, got {nav_in_output}"
        assert len(pushed[0]) == 50


class TestLastfmFallback:
    @pytest.mark.asyncio
    async def test_lastfm_used_when_lb_yields_nothing(self, monkeypatch, trigger):
        pushed = []
        client = _make_client(pushed)
        db = _make_db(trigger, lb_configured=True, lastfm_configured=True)

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())
        # ListenBrainz configured but empty (e.g. a brand-new account).
        monkeypatch.setattr(scheduler, "_gather_lb_candidates", AsyncMock(return_value=[]))

        site = MagicMock()
        site.get_recent_tracks = AsyncMock(return_value=[])
        site.close = AsyncMock()
        built = []

        def make_lfm(*a, **k):
            built.append(1)
            return site

        monkeypatch.setattr(scheduler, "LastfmClient", make_lfm)

        await scheduler.run_recency_trigger(1)

        assert built, "Last.fm was not tried even though ListenBrainz was empty"
        assert pushed, "no playlist pushed"

    @pytest.mark.asyncio
    async def test_unconfigured_everything_still_builds_from_newest(self, monkeypatch, trigger):
        pushed = []
        client = _make_client(pushed)
        db = _make_db(trigger, lb_configured=False, lastfm_configured=False)

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())

        await scheduler.run_recency_trigger(1)

        assert pushed, "no playlist pushed with no history sources"
        # Thin history must still yield a full playlist from newest tracks.
        assert len(pushed[0]) == 25


class TestGatherCandidates:
    @pytest.mark.asyncio
    async def test_returns_empty_when_not_configured(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        assert await scheduler._gather_lb_candidates(db, MagicMock(), 1) == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_client_error(self, monkeypatch):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            username="u", user_token="t"
        )
        lb = MagicMock()
        lb.get_recent_listens = AsyncMock(side_effect=RuntimeError("boom"))
        lb.close = AsyncMock()
        monkeypatch.setattr(scheduler, "ListenBrainzClient", lambda *a, **k: lb)

        assert await scheduler._gather_lb_candidates(db, MagicMock(), 1) == []
        lb.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_closes_client_even_when_no_listens(self, monkeypatch):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            username="u", user_token="t"
        )
        lb = MagicMock()
        lb.get_recent_listens = AsyncMock(return_value=[])
        lb.close = AsyncMock()
        monkeypatch.setattr(scheduler, "ListenBrainzClient", lambda *a, **k: lb)

        assert await scheduler._gather_lb_candidates(db, MagicMock(), 1) == []
        lb.close.assert_awaited()


class TestSearchFallback:
    @pytest.mark.asyncio
    async def test_resolves_via_navidrome_search(self):
        client = MagicMock()
        client.search = AsyncMock(return_value={"searchResult3": {"song": [{"id": "found1"}]}})

        out = await scheduler._resolve_via_search(
            client, [{"artist": "A", "title": "B", "recording_mbid": None}],
        )
        assert out == ["found1"]

    @pytest.mark.asyncio
    async def test_skips_mbid_only_entries(self):
        client = MagicMock()
        client.search = AsyncMock()
        out = await scheduler._resolve_via_search(
            client, [{"artist": "", "title": "", "recording_mbid": "m"}],
        )
        assert out == []
        client.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_caps_the_number_of_searches(self):
        client = MagicMock()
        client.search = AsyncMock(return_value={"searchResult3": {"song": [{"id": "x"}]}})
        entries = [{"artist": "A", "title": f"T{i}", "recording_mbid": None} for i in range(50)]

        await scheduler._resolve_via_search(client, entries, cap=10)
        assert client.search.await_count == 10

    @pytest.mark.asyncio
    async def test_survives_search_errors(self):
        client = MagicMock()
        client.search = AsyncMock(side_effect=RuntimeError("nav down"))
        out = await scheduler._resolve_via_search(
            client, [{"artist": "A", "title": "B", "recording_mbid": None}],
        )
        assert out == []
