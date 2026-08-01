"""Tests for Smart Triggers scheduler service."""
import json
from datetime import datetime, timedelta
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from app.services.scheduler import (
    init_scheduler,
    add_trigger_job,
    remove_trigger_job,
    pause_trigger_job,
    resume_trigger_job,
    TRIGGER_JOBS,
    _get_cooldown_track_ids,
)
from app.models.database import SmartTrigger, GeneratedPlaylist


@pytest.fixture
def mock_scheduler():
    """Create a real APScheduler with a mock jobstore for testing."""
    sched = MagicMock(spec=AsyncIOScheduler)
    sched.add_job = MagicMock()
    sched.remove_job = MagicMock()
    sched.pause_job = MagicMock()
    sched.resume_job = MagicMock()
    return sched


@pytest.fixture
def scheduler_service(mock_scheduler):
    """Init the scheduler service with our mock."""
    init_scheduler(mock_scheduler)
    return mock_scheduler


def make_trigger(
    trigger_id=1,
    name="Test Trigger",
    trigger_type="recency",
    enabled=True,
    cron_expression=None,
    threshold=None,
    playlist_name=None,
):
    t = MagicMock(spec=SmartTrigger)
    t.id = trigger_id
    t.name = name
    t.trigger_type = trigger_type
    t.enabled = enabled
    t.cron_expression = cron_expression
    t.threshold = threshold
    t.playlist_name = playlist_name
    return t


class TestAddTriggerJob:
    def test_add_recency_trigger(self, scheduler_service):
        """Recency trigger should be scheduled with daily cron."""
        trigger = make_trigger(trigger_type="recency")
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_called_once()
        call_args = scheduler_service.add_job.call_args
        assert call_args[1]["id"] == "trigger-1"
        assert call_args[1]["args"] == [1]

    def test_add_heavy_rotation_trigger(self, scheduler_service):
        """Heavy rotation trigger should be scheduled every 6 hours."""
        trigger = make_trigger(trigger_type="heavy_rotation")
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_called_once()
        call_args = scheduler_service.add_job.call_args
        assert call_args[1]["id"] == "trigger-1"

    def test_add_scheduled_trigger_with_cron(self, scheduler_service):
        """Scheduled trigger should use the provided cron expression."""
        trigger = make_trigger(
            trigger_type="scheduled",
            cron_expression="0 17 * * 5",
        )
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_called_once()
        call_args = scheduler_service.add_job.call_args
        assert call_args[1]["id"] == "trigger-1"

    def test_add_scheduled_trigger_invalid_cron(self, scheduler_service):
        """Invalid cron expression should not create a job."""
        trigger = make_trigger(
            trigger_type="scheduled",
            cron_expression="invalid",
        )
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_not_called()

    def test_disabled_trigger_is_paused(self, scheduler_service):
        """Disabled trigger should be added then immediately paused."""
        trigger = make_trigger(enabled=False)
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_called_once()
        scheduler_service.pause_job.assert_called_once_with("trigger-1")

    def test_enabled_trigger_not_paused(self, scheduler_service):
        """Enabled trigger should not be paused after adding."""
        trigger = make_trigger(enabled=True)
        add_trigger_job(trigger)
        scheduler_service.pause_job.assert_not_called()

    def test_unknown_trigger_type_skipped(self, scheduler_service):
        """Unknown trigger type should not create a job."""
        trigger = make_trigger(trigger_type="unknown_type")
        add_trigger_job(trigger)
        scheduler_service.add_job.assert_not_called()


class TestRemoveTriggerJob:
    def test_remove_trigger(self, scheduler_service):
        remove_trigger_job(42)
        scheduler_service.remove_job.assert_called_once_with("trigger-42")

    def test_remove_nonexistent_no_error(self, scheduler_service):
        """Removing a non-existent job should not raise."""
        scheduler_service.remove_job.side_effect = Exception("not found")
        remove_trigger_job(999)  # Should not raise


class TestPauseTriggerJob:
    def test_pause(self, scheduler_service):
        pause_trigger_job(1)
        scheduler_service.pause_job.assert_called_once_with("trigger-1")

    def test_pause_nonexistent_no_error(self, scheduler_service):
        scheduler_service.pause_job.side_effect = Exception("not found")
        pause_trigger_job(999)


class TestResumeTriggerJob:
    def test_resume(self, scheduler_service):
        resume_trigger_job(1)
        scheduler_service.resume_job.assert_called_once_with("trigger-1")

    def test_resume_nonexistent_no_error(self, scheduler_service):
        scheduler_service.resume_job.side_effect = Exception("not found")
        resume_trigger_job(999)


class TestTriggerJobMapping:
    def test_all_types_mapped(self):
        """All trigger types should have a job function."""
        assert "recency" in TRIGGER_JOBS
        assert "heavy_rotation" in TRIGGER_JOBS
        assert "scheduled" in TRIGGER_JOBS

    def test_job_functions_are_coroutines(self):
        """Job functions should be async."""
        import inspect
        for name, func in TRIGGER_JOBS.items():
            assert inspect.iscoroutinefunction(func), f"{name} should be async"


def _make_playlist(name: str, track_ids: list, updated_at: datetime) -> MagicMock:
    """Build a mock GeneratedPlaylist row with the given name/track_ids/updated_at."""
    p = MagicMock(spec=GeneratedPlaylist)
    p.name = name
    p.track_ids = json.dumps(track_ids)
    p.updated_at = updated_at
    return p


def _bind_rows(rows):
    """Build a MagicMock db where .query().filter().filter().all() returns `rows`.

    The helper applies two filters (updated_at cutoff, name LIKE prefix), so the
    mock chain must reflect that.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = rows
    return db


class TestCooldownHelper:
    """Tests for _get_cooldown_track_ids: returns tracks selected in the last N days
    whose GeneratedPlaylist name matches the prefix. Used to dedupe recency-trigger
    output across consecutive days so the same song doesn't reappear day after day."""

    def test_no_recent_playlists_returns_empty(self):
        """No matching rows in cooldown window → empty set."""
        db = _bind_rows([])
        result = _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        assert result == set()

    def test_single_recent_playlist_returns_its_tracks(self):
        """A matching playlist updated today → its track_ids are in cooldown."""
        db = _bind_rows([
            _make_playlist("Fresh Discoveries 2026-07-31", ["a", "b", "c"], datetime.utcnow()),
        ])
        result = _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        assert result == {"a", "b", "c"}

    def test_multiple_recent_playlists_union_tracks(self):
        """Multiple matching rows → union of all track_ids."""
        db = _bind_rows([
            _make_playlist("Fresh Discoveries 2026-07-31", ["a", "b"], datetime.utcnow()),
            _make_playlist("Fresh Discoveries 2026-07-30", ["b", "c", "d"], datetime.utcnow() - timedelta(days=1)),
        ])
        result = _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        assert result == {"a", "b", "c", "d"}

    def test_playlist_with_null_track_ids_skipped(self):
        """Rows with track_ids=None must not raise — they just contribute nothing."""
        null_pl = MagicMock(spec=GeneratedPlaylist)
        null_pl.name = "Fresh Discoveries 2026-07-30"
        null_pl.track_ids = None
        null_pl.updated_at = datetime.utcnow() - timedelta(days=1)
        db = _bind_rows([
            null_pl,
            _make_playlist("Fresh Discoveries 2026-07-31", ["x"], datetime.utcnow()),
        ])
        result = _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        assert result == {"x"}

    def test_malformed_track_ids_json_does_not_raise(self):
        """A row with garbage in track_ids must be skipped, not crash the trigger."""
        bad = MagicMock(spec=GeneratedPlaylist)
        bad.name = "Fresh Discoveries 2026-07-30"
        bad.track_ids = "not-json{"
        bad.updated_at = datetime.utcnow() - timedelta(days=1)
        db = _bind_rows([
            bad,
            _make_playlist("Fresh Discoveries 2026-07-31", ["ok"], datetime.utcnow()),
        ])
        result = _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        assert result == {"ok"}

    def test_filter_uses_name_prefix_and_window(self):
        """The SQL filter chain must apply BOTH updated_at cutoff AND name LIKE prefix."""
        db = _bind_rows([])
        _get_cooldown_track_ids(db, "Fresh Discoveries", days=2)
        # Two .filter() calls: one for updated_at cutoff, one for name LIKE
        db.query.return_value.filter.assert_called_once()
        db.query.return_value.filter.return_value.filter.assert_called_once()

    def test_default_window_is_two_days(self):
        """Default days argument is 2 (matches 'next two days' requirement)."""
        import inspect
        sig = inspect.signature(_get_cooldown_track_ids)
        assert sig.parameters["days"].default == 2


class TestRecencyTriggerCooldownPrefix:
    """Verify the prefix-stripping logic that derives the cooldown key from the playlist name.

    Default recency playlists are named 'Fresh Discoveries YYYY-MM-DD' (one per day).
    To match prior days' playlists, the trigger must strip the date suffix and use
    'Fresh Discoveries' as the prefix. Custom playlist names without a date are
    used as-is, which means they cooldown against themselves (still correct, since
    the trigger overwrites the same playlist each run).
    """

    def test_default_name_strips_date_suffix(self):
        """'Fresh Discoveries 2026-07-31' -> prefix 'Fresh Discoveries'."""
        from app.services.scheduler import _cooldown_prefix_for
        playlist_name = f"Fresh Discoveries {datetime.utcnow().strftime('%Y-%m-%d')}"
        assert _cooldown_prefix_for(playlist_name) == "Fresh Discoveries"

    def test_custom_name_without_date_kept_as_is(self):
        """A custom static name like 'My Mix' has no date to strip."""
        from app.services.scheduler import _cooldown_prefix_for
        assert _cooldown_prefix_for("My Mix") == "My Mix"

    def test_name_with_extra_whitespace_before_date_still_strips(self):
        """Defensive: trailing whitespace before the date still works."""
        from app.services.scheduler import _cooldown_prefix_for
        assert _cooldown_prefix_for("Fresh Discoveries  2026-07-31") == "Fresh Discoveries"

    def test_empty_string_returns_empty(self):
        """An empty playlist name should not crash the regex."""
        from app.services.scheduler import _cooldown_prefix_for
        assert _cooldown_prefix_for("") == ""


class TestRecencyTriggerCooldownIntegration:
    """End-to-end: the recency trigger must exclude tracks that were in the
    last 2 days' 'Fresh Discoveries' playlists, so the same song doesn't
    reappear day after day."""

    @pytest.mark.asyncio
    async def test_cooldown_set_excluded_from_final_track_ids(self, monkeypatch):
        """If a song was in yesterday's Fresh Discoveries, it must not appear in today's output."""
        from app.services.scheduler import run_recency_trigger
        from app.models.database import (
            SmartTrigger, ConnectionConfig, LastfmConfig,
            GeneratedPlaylist, SessionLocal,
        )

        # Build a trigger row
        trigger = MagicMock(spec=SmartTrigger)
        trigger.id = 1
        trigger.name = "Daily Mix"
        trigger.trigger_type = "recency"
        trigger.enabled = True
        trigger.playlist_name = None  # use default
        trigger.last_run = None

        # Pre-existing playlist: yesterday's selection includes "blocked1", "blocked2"
        existing = MagicMock(spec=GeneratedPlaylist)
        existing.name = "Fresh Discoveries 2026-07-30"
        existing.navidrome_playlist_id = "old-pid"
        existing.track_ids = json.dumps(["blocked1", "blocked2"])
        existing.track_count = 2
        existing.updated_at = datetime.utcnow()

        # We'll capture what track_ids the trigger tries to push to Navidrome
        pushed_tracks = []
        created_playlist_row = None

        def fake_query(model):
            m = MagicMock()
            if model is SmartTrigger:
                m.filter.return_value.first.return_value = trigger
            elif model is LastfmConfig:
                m.filter.return_value.first.return_value = None  # no lastfm configured
            elif model is ConnectionConfig:
                m.filter.return_value.first.return_value = MagicMock(
                    url="http://nav", username="u", password="p"
                )
            elif model is GeneratedPlaylist:
                # The cooldown helper (mocked) and same-name lookup both query this.
                m.filter.return_value.first.return_value = existing
                m.filter.return_value.filter.return_value.all.return_value = [existing]
            else:
                m.filter.return_value.first.return_value = None
            return m

        db_mock = MagicMock()
        db_mock.query.side_effect = fake_query
        # commit/rollback/add are no-ops on MagicMock
        db_mock.commit = MagicMock()
        db_mock.rollback = MagicMock()
        db_mock.add = MagicMock()
        db_mock.refresh = MagicMock()

        monkeypatch.setattr("app.services.scheduler.SessionLocal", lambda: db_mock)

        # Mock the cooldown helper to return the known blocked set
        monkeypatch.setattr(
            "app.services.scheduler._get_cooldown_track_ids",
            lambda db, prefix, days=2: {"blocked1", "blocked2"},
        )

        # Mock NavidromeClient
        fake_client = MagicMock()
        fake_client.close = AsyncMock()
        fake_client.get_album_list2 = AsyncMock(return_value=[
            {"id": "album-1", "name": "Album One"},
            {"id": "album-2", "name": "Album Two"},
        ])
        # Album 1 contains: "fresh1", "fresh2", "blocked1" (must be filtered)
        # Album 2 contains: "fresh3", "blocked2" (must be filtered), "fresh4"
        async def fake_get_album(album_id):
            if album_id == "album-1":
                return {"album": {"song": [
                    {"id": "fresh1", "artist_name": "Artist A"},
                    {"id": "fresh2", "artist_name": "Artist B"},
                    {"id": "blocked1", "artist_name": "Artist C"},
                ]}}
            return {"album": {"song": [
                {"id": "fresh3", "artist_name": "Artist A"},
                {"id": "blocked2", "artist_name": "Artist D"},
                {"id": "fresh4", "artist_name": "Artist E"},
            ]}}
        fake_client.get_album = fake_get_album

        async def fake_create_playlist(name, track_ids):
            pushed_tracks.append(list(track_ids))
            return {"playlist": {"id": "new-pid"}}

        fake_client.create_playlist = fake_create_playlist
        fake_client.delete_playlist = AsyncMock()

        monkeypatch.setattr("app.services.scheduler._get_client_from_db", lambda: fake_client)

        # Run the trigger
        await run_recency_trigger(1)

        # The push to Navidrome must not include any blocked track
        assert pushed_tracks, "Trigger did not push a playlist"
        final = set(pushed_tracks[0])
        assert "blocked1" not in final, f"blocked1 leaked into output: {final}"
        assert "blocked2" not in final, f"blocked2 leaked into output: {final}"
        # And it must have included some fresh tracks
        assert final & {"fresh1", "fresh2", "fresh3", "fresh4"}, f"No fresh tracks: {final}"
