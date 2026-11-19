"""Heavy-rotation source routing and thin-history bootstrap tests."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.scheduler as scheduler
from app.models.database import (
    ConnectionConfig,
    GeneratedPlaylist,
    LastfmConfig,
    ListenbrainzConfig,
    SmartTrigger,
)


@pytest.fixture
def trigger():
    t = MagicMock(spec=SmartTrigger)
    t.id = 1
    t.name = "Heavy"
    t.trigger_type = "heavy_rotation"
    t.enabled = True
    t.playlist_name = None
    t.last_run = None
    t.threshold = 5
    return t


def _make_db(trigger, *, lb_configured: bool, lastfm_configured: bool):
    def fake_query(model):
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


def _hot_client(pushed):
    client = MagicMock()
    client.close = AsyncMock()
    client.get_album_list2 = AsyncMock(return_value=[{"id": "a1"}])
    client.get_album = AsyncMock(return_value={"album": {"song": [
        {"id": "hot1", "title": "Hot Song", "artist": "Artist", "playCount": 10},
    ]}})
    client.get_similar_songs2 = AsyncMock(return_value=[])
    client.search = AsyncMock(return_value={"searchResult3": {"song": [{"id": "lfm1"}]}})
    client.delete_playlist = AsyncMock()
    client.update_playlist = AsyncMock()

    async def create_playlist(name, ids):
        pushed.append(list(ids))
        return {"playlist": {"id": "pid"}}

    client.create_playlist = create_playlist
    return client


class TestLbSimilarForTrack:
    """_lb_similar_for_track: MBID lookup + Labs call + 7d cache."""

    def _db(self, mbid):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            recording_mbid=mbid
        )
        return db

    @pytest.mark.asyncio
    async def test_returns_empty_without_mbid(self):
        lb = MagicMock()
        lb.get_similar_recordings = AsyncMock()
        out = await scheduler._lb_similar_for_track(self._db(None), lb, MagicMock(), "s1")
        assert out == []
        lb.get_similar_recordings.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_cache_without_calling_labs(self, monkeypatch):
        lb = MagicMock()
        lb.get_similar_recordings = AsyncMock()
        monkeypatch.setattr(
            scheduler, "get_cached_similar",
            lambda d, m: [{"recording_mbid": "sim1", "recording_name": "S",
                           "artist_credit_name": "A"}],
        )
        monkeypatch.setattr(scheduler, "resolve_entries", lambda d, e: (["nav1"], []))

        out = await scheduler._lb_similar_for_track(self._db("mbid-1"), lb, MagicMock(), "s1")

        assert out == ["nav1"]
        lb.get_similar_recordings.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetches_and_caches_on_miss(self, monkeypatch):
        rows = [{"recording_mbid": "sim1", "recording_name": "S",
                 "artist_credit_name": "A"}]
        lb = MagicMock()
        lb.get_similar_recordings = AsyncMock(return_value=rows)
        client = MagicMock()
        client.search = AsyncMock(return_value={"searchResult3": {"song": []}})
        stored = []
        monkeypatch.setattr(scheduler, "get_cached_similar", lambda d, m: None)
        monkeypatch.setattr(scheduler, "put_similar", lambda d, m, p: stored.append((m, p)))
        monkeypatch.setattr(scheduler, "resolve_entries", lambda d, e: (["nav1"], []))

        await scheduler._lb_similar_for_track(self._db("mbid-1"), lb, client, "s1")

        assert stored == [("mbid-1", rows)]

    @pytest.mark.asyncio
    async def test_labs_error_degrades_to_empty(self, monkeypatch):
        lb = MagicMock()
        lb.get_similar_recordings = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(scheduler, "get_cached_similar", lambda d, m: None)

        out = await scheduler._lb_similar_for_track(self._db("mbid-1"), lb, MagicMock(), "s1")

        assert out == []

    @pytest.mark.asyncio
    async def test_respects_limit(self, monkeypatch):
        lb = MagicMock()
        monkeypatch.setattr(
            scheduler, "get_cached_similar",
            lambda d, m: [{"recording_mbid": f"s{i}", "recording_name": f"T{i}",
                           "artist_credit_name": "A"} for i in range(50)],
        )
        monkeypatch.setattr(
            scheduler, "resolve_entries",
            lambda d, e: ([f"nav{i}" for i in range(50)], []),
        )

        out = await scheduler._lb_similar_for_track(
            self._db("mbid-1"), lb, MagicMock(), "s1", limit=20,
        )
        assert len(out) == 20


class TestHeavyRotationSources:
    @pytest.mark.asyncio
    async def test_lastfm_similar_skipped_when_lb_covers(self, monkeypatch, trigger):
        pushed = []
        client = _hot_client(pushed)
        client.get_similar_songs2 = AsyncMock(
            return_value=[{"id": f"nav{i}"} for i in range(20)]
        )

        db = _make_db(trigger, lb_configured=True, lastfm_configured=True)
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(
            scheduler, "_lb_similar_for_track",
            AsyncMock(return_value=[f"lb{i}" for i in range(20)]),
        )

        lfm = MagicMock()
        lfm.close = AsyncMock()
        lfm.get_similar = AsyncMock(return_value=[])
        monkeypatch.setattr(scheduler, "LastfmClient", lambda *a, **k: lfm)

        await scheduler.run_heavy_rotation_trigger(1)

        lfm.get_similar.assert_not_awaited()
        assert pushed, "heavy rotation pushed no playlist"
        # 60% of the 20 per-seed slots come from ListenBrainz.
        assert sum(1 for t in pushed[0] if str(t).startswith("lb")) == 12

    @pytest.mark.asyncio
    async def test_lastfm_similar_used_as_fallback(self, monkeypatch, trigger):
        pushed = []
        client = _hot_client(pushed)

        db = _make_db(trigger, lb_configured=True, lastfm_configured=True)
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        # ListenBrainz knows nothing for this seed.
        monkeypatch.setattr(scheduler, "_lb_similar_for_track", AsyncMock(return_value=[]))

        lfm = MagicMock()
        lfm.close = AsyncMock()
        lfm.get_similar = AsyncMock(return_value=[
            {"artist": {"#text": "Other"}, "name": "Song"},
        ])
        monkeypatch.setattr(scheduler, "LastfmClient", lambda *a, **k: lfm)

        await scheduler.run_heavy_rotation_trigger(1)

        lfm.get_similar.assert_awaited()
        assert pushed


class TestThinHistoryBootstrap:
    @pytest.mark.asyncio
    async def test_thin_history_still_yields_full_playlist(self, monkeypatch, trigger):
        """A near-empty ListenBrainz account must not produce a 23-track '50'."""
        pushed = []
        client = MagicMock()
        client.close = AsyncMock()
        client.get_album_list2 = AsyncMock(return_value=[{"id": "a1"}])
        client.get_album = AsyncMock(return_value={"album": {"song": [
            {"id": f"new{i}", "artist_name": f"Artist {i}"} for i in range(60)
        ]}})
        client.get_similar_songs2 = AsyncMock(return_value=[])
        client.delete_playlist = AsyncMock()

        async def create_playlist(name, ids):
            pushed.append(list(ids))
            return {"playlist": {"id": "pid"}}

        client.create_playlist = create_playlist

        db = _make_db(trigger, lb_configured=True, lastfm_configured=False)
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
        monkeypatch.setattr(scheduler, "_get_client_from_db", lambda: client)
        monkeypatch.setattr(scheduler, "_get_cooldown_track_ids", lambda *a, **k: set())
        monkeypatch.setattr(
            scheduler, "_gather_lb_candidates",
            AsyncMock(return_value=[{"artist": "x", "title": "y", "recording_mbid": "m"}]),
        )
        # Only 3 history tracks — well under the 30 needed for a full mix.
        monkeypatch.setattr(
            scheduler, "resolve_entries", lambda d, e: (["lb0", "lb1", "lb2"], []),
        )

        await scheduler.run_recency_trigger(1)

        assert pushed
        assert len(pushed[0]) == 50, f"expected 50 tracks, got {len(pushed[0])}"
        assert len({"lb0", "lb1", "lb2"}.intersection(pushed[0])) > 0
