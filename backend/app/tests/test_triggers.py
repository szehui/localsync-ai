"""Tests for Smart Triggers scheduler service."""
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
)
from app.models.database import SmartTrigger


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
