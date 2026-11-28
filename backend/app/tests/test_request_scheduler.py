"""Scheduling the request reconciler.

The wiring matters more than it looks: a reconciler that never runs leaves every
request frozen at `approved`, and one that runs twice at once can write the same
row from two sessions. These tests pin the registration contract without starting
a real scheduler.
"""
from unittest.mock import MagicMock

import pytest

from app.services import scheduler


@pytest.fixture(autouse=True)
def _reset_scheduler_ref():
    """Every test starts with no scheduler, and leaves none behind."""
    original = scheduler._scheduler
    scheduler._scheduler = None
    try:
        yield
    finally:
        scheduler._scheduler = original


def test_registration_is_a_no_op_without_a_running_scheduler():
    """Importing the app must not register a job with nothing to run it."""
    assert scheduler.add_request_sync_job() is False


def test_job_is_registered_with_overlap_protection():
    fake = MagicMock()
    scheduler._scheduler = fake

    assert scheduler.add_request_sync_job(minutes=5) is True

    kwargs = fake.add_job.call_args.kwargs
    assert kwargs["id"] == scheduler.REQUEST_SYNC_JOB_ID
    assert kwargs["minutes"] == 5
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1
    assert kwargs["replace_existing"] is True


def test_job_id_is_stable_so_restarts_replace_instead_of_duplicating():
    """The job store is persistent; a new id per boot would pile up copies."""
    assert scheduler.REQUEST_SYNC_JOB_ID == "request-reconcile"


def test_the_scheduled_function_is_the_async_job():
    fake = MagicMock()
    scheduler._scheduler = fake
    scheduler.add_request_sync_job()

    from app.services.request_sync import reconcile_job

    assert fake.add_job.call_args.args[0] is reconcile_job
