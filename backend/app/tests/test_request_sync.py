"""The reconciler: one fixture per row of the transition table, plus the guards.

The interesting failures here are not exceptions — they are *false statements*.
A request that says `available` when Navidrome cannot serve the album, or
`failed` because one cycle saw an empty queue, is worse than no status at all:
the user stops looking. So the tests are mostly about what the reconciler
refuses to claim.

Lidarr is faked at the client boundary (queue, history, album lookup) because
those payload shapes were captured from a live 3.1.6 instance; everything below
that is the real implementation.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import Album, Base, Request, User
from app.services import request_sync as rs
from app.services.requests import OPEN_STATUSES, STATUS_RANK

RG = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 11, 28, 12, 0, 0)

MBID_FULL = "aaaaaaaa-0000-0000-0000-000000000001"
MBID_PARTIAL = "aaaaaaaa-0000-0000-0000-000000000002"
MBID_QUEUED = "aaaaaaaa-0000-0000-0000-000000000003"
MBID_FAILED = "aaaaaaaa-0000-0000-0000-000000000004"
MBID_VANISHED = "aaaaaaaa-0000-0000-0000-000000000005"
MBID_STALE = "aaaaaaaa-0000-0000-0000-000000000006"
MBID_WAITING = "aaaaaaaa-0000-0000-0000-000000000007"
MBID_OWNED = "aaaaaaaa-0000-0000-0000-000000000008"
MBID_PENDING = "aaaaaaaa-0000-0000-0000-000000000009"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def owner(db):
    user = User(username="sze", display_name="sze", navidrome_url="http://nav",
                navidrome_password="x", is_owner=True)
    db.add(user)
    db.commit()
    return user


def album_row(mbid, *, album_id, percent=0, last_search=None, release_date=None,
              track_count=12):
    """An album payload shaped like `GET /api/v1/album`. Captured live."""
    stats = {
        "trackFileCount": int(track_count * percent / 100),
        "trackCount": track_count,
        "totalTrackCount": track_count,
        "sizeOnDisk": 0,
        "percentOfTracks": percent,
    }
    row = {
        "id": album_id,
        "foreignAlbumId": mbid,
        "monitored": True,
        "statistics": stats,
        "lastSearchTime": last_search,
        "releaseDate": release_date,
        "artistId": 505,
    }
    return row


def make_request(db, user, mbid, status="approved", title="An Album",
                 artist="An Artist", requested_at=None, kind="album"):
    request = Request(
        user_id=user.id,
        kind=kind,
        release_group_mbid=mbid,
        artist_mbid="artist-mbid",
        artist_name=artist,
        album_title=title,
        status=status,
        requested_at=requested_at or (NOW - timedelta(hours=6)),
    )
    db.add(request)
    db.commit()
    return request


class FakeLidarr:
    """Lidarr's client surface, as the reconciler uses it."""

    def __init__(self, albums=None, queue=None, history=None):
        self.albums = albums or {}
        self.queue_records = queue or []
        self.history_records = history or []
        self.queue_calls = 0
        self.history_calls = 0

    async def find_album_by_release_group(self, mbid):
        return self.albums.get(mbid)

    async def get_queue(self):
        self.queue_calls += 1
        return self.queue_records

    async def get_history(self):
        self.history_calls += 1
        return self.history_records


# ── The transition table ────────────────────────────────────────────────────


class TestDecisionTable:
    def _decide(self, status, **kwargs):
        base = dict(available_evidence=None, album=album_row(RG, album_id=1),
                    in_queue=False, failure=None,
                    requested_at=NOW - timedelta(hours=6), now=NOW)
        base.update(kwargs)
        return rs.decide(status, **base)

    def test_in_library_wins_over_everything(self):
        status, evidence, error = self._decide(
            "downloading", available_evidence="mbid", in_queue=True)
        assert (status, evidence, error) == ("available", "mbid", None)

    def test_files_in_lidarr_but_not_scanned_is_imported_not_available(self):
        """The library cannot serve it yet, so 'available' would be a lie."""
        status, _, _ = self._decide(
            "downloading", album=album_row(RG, album_id=1, percent=100))
        assert status == "imported"

    def test_partial_import_is_partially_available(self):
        status, _, _ = self._decide(
            "downloading", album=album_row(RG, album_id=1, percent=40))
        assert status == "partially_available"

    def test_queue_entry_is_downloading(self):
        assert self._decide("approved", in_queue=True)[0] == "downloading"

    def test_failure_after_request_is_failed_with_reason(self):
        status, _, error = self._decide(
            "downloading",
            failure=(NOW - timedelta(hours=1), "downloadFailed"))
        assert status == "failed"
        assert "downloadFailed" in error

    def test_failure_before_request_is_ignored(self):
        """A failed download from before the ask says nothing about this ask."""
        status, _, _ = self._decide(
            "approved", failure=(NOW - timedelta(hours=9), "downloadFailed"))
        assert status == "approved"

    def test_album_missing_from_lidarr_is_failed(self):
        status, _, error = self._decide("approved", album=None)
        assert status == "failed"
        assert "no longer in Lidarr" in error

    def test_stale_search_with_no_files_is_failed(self):
        status, _, error = self._decide(
            "approved",
            album=album_row(RG, album_id=1,
                            last_search=(NOW - timedelta(days=3))
                            .strftime("%Y-%m-%dT%H:%M:%SZ")))
        assert status == "failed"
        assert "No release found" in error

    def test_unreleased_album_is_not_a_failure(self):
        """Lidarr found nothing because the album is not out yet."""
        status, _, _ = self._decide(
            "approved",
            album=album_row(RG, album_id=1,
                            last_search=(NOW - timedelta(days=30))
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
                            release_date="2027-06-01T00:00:00Z"))
        assert status == "approved"

    def test_pending_is_not_approved_by_a_stray_lidarr_row(self):
        """`POST /album` creates the whole discography — an unmonitored sibling
        row is not evidence that this request was approved."""
        status, _, _ = self._decide("pending", in_queue=True,
                                    album=album_row(RG, album_id=1, percent=50))
        assert status == "pending"

    def test_pending_can_become_available(self):
        """Being in the library is evidence no matter who asked or when."""
        assert self._decide("pending", available_evidence="name")[0] == "available"


# ── No backwards movement ───────────────────────────────────────────────────


class TestMonotonic:
    @pytest.mark.parametrize("current,target", [
        ("available", "downloading"),
        ("available", "approved"),
        ("imported", "downloading"),
        ("partially_available", "approved"),
        ("downloading", "approved"),
        ("denied", "approved"),
        ("cancelled", "approved"),
    ])
    def test_backwards_transitions_are_refused(self, current, target):
        assert rs._may_advance(current, target) is False

    @pytest.mark.parametrize("current,target", [
        ("approved", "downloading"),
        ("downloading", "imported"),
        ("imported", "available"),
        ("approved", "partially_available"),
    ])
    def test_forward_transitions_are_allowed(self, current, target):
        assert rs._may_advance(current, target) is True

    def test_failed_can_be_left_on_positive_evidence(self):
        assert rs._may_advance("failed", "available") is True
        assert rs._may_advance("failed", "downloading") is True

    def test_failed_cannot_become_approved(self):
        """Silence must not undo a failure — that is what retry is for."""
        assert rs._may_advance("failed", "approved") is False

    def test_imported_never_becomes_failed(self):
        """An album with files is not a failure, whatever history says."""
        assert rs._may_advance("imported", "failed") is False

    def test_rank_covers_every_reconciled_status(self):
        for status in OPEN_STATUSES:
            assert status in STATUS_RANK


# ── The cycle ───────────────────────────────────────────────────────────────


class TestCycle:
    async def test_idle_household_makes_no_lidarr_calls(self, db, owner):
        lidarr = FakeLidarr()
        result = await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        assert result == {"total": 0, "changed": 0, "failed": 0}
        assert (lidarr.queue_calls, lidarr.history_calls) == (0, 0)

    async def test_queue_and_history_are_fetched_once_for_many_requests(
            self, db, owner):
        make_request(db, owner, MBID_QUEUED)
        make_request(db, owner, MBID_WAITING)
        make_request(db, owner, MBID_STALE)
        lidarr = FakeLidarr(albums={
            MBID_QUEUED: album_row(MBID_QUEUED, album_id=3),
            MBID_WAITING: album_row(MBID_WAITING, album_id=7),
            MBID_STALE: album_row(MBID_STALE, album_id=6),
        }, queue=[{"albumId": 3, "status": "downloading"}])

        await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)

        assert (lidarr.queue_calls, lidarr.history_calls) == (1, 1)

    async def test_full_ladder_in_one_cycle(self, db, owner):
        full = make_request(db, owner, MBID_FULL)
        partial = make_request(db, owner, MBID_PARTIAL)
        queued = make_request(db, owner, MBID_QUEUED)
        failed = make_request(db, owner, MBID_FAILED)
        vanished = make_request(db, owner, MBID_VANISHED)
        waiting = make_request(db, owner, MBID_WAITING)

        lidarr = FakeLidarr(
            albums={
                MBID_FULL: album_row(MBID_FULL, album_id=1, percent=100),
                MBID_PARTIAL: album_row(MBID_PARTIAL, album_id=2, percent=40),
                MBID_QUEUED: album_row(MBID_QUEUED, album_id=3),
                MBID_FAILED: album_row(MBID_FAILED, album_id=4),
                # MBID_VANISHED intentionally absent
                MBID_WAITING: album_row(MBID_WAITING, album_id=7),
            },
            queue=[{"albumId": 3, "status": "downloading"}],
            history=[{"albumId": 4, "eventType": "downloadFailed",
                      "date": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}],
        )

        result = await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()

        assert db.get(Request, full.id).status == "imported"
        assert db.get(Request, partial.id).status == "partially_available"
        assert db.get(Request, queued.id).status == "downloading"
        assert db.get(Request, failed.id).status == "failed"
        assert "downloadFailed" in db.get(Request, failed.id).last_error
        assert db.get(Request, vanished.id).status == "failed"
        assert db.get(Request, waiting.id).status == "approved"
        assert result["total"] == 6
        assert result["changed"] == 5
        assert result["failed"] == 2

    async def test_library_hit_becomes_available_with_evidence(self, db, owner):
        request = make_request(db, owner, MBID_OWNED)
        db.add(Album(id="nav-1", name="An Album", artist_name="An Artist",
                     song_count=12, release_group_mbid=MBID_OWNED))
        db.commit()

        await rs.reconcile_open_requests(db, lidarr=FakeLidarr(), now=NOW)
        db.expire_all()

        row = db.get(Request, request.id)
        assert row.status == "available"
        assert row.available_evidence == "mbid"

    async def test_name_match_without_a_lidarr_row_falls_back_to_name(
            self, db, owner):
        """Documented looseness: `requests` stores no track count.

        With no Lidarr row to ask, artist+title folding is the only evidence
        there is — and reporting "not owned" would generate a duplicate add for
        an album the household already has. The catalog cards make the same
        call, so the two agree.
        """
        request = make_request(db, owner, MBID_OWNED, title="Greatest Hits",
                               artist="The Band")
        db.add(Album(id="nav-2", name="Greatest Hits", artist_name="The Band",
                     song_count=30))
        db.commit()

        await rs.reconcile_open_requests(db, lidarr=FakeLidarr(), now=NOW)
        db.expire_all()
        assert db.get(Request, request.id).status == "available"

    async def test_name_match_with_mbid_less_rip_is_available(self, db, owner):
        """The untagged-single-track floor: no MBID, but the name agrees."""
        request = make_request(db, owner, MBID_OWNED, title="An Album",
                               artist="An Artist")
        db.add(Album(id="nav-3", name="An Album", artist_name="An Artist",
                     song_count=11))  # one bonus track off
        db.commit()

        await rs.reconcile_open_requests(db, lidarr=FakeLidarr(), now=NOW)
        db.expire_all()
        row = db.get(Request, request.id)
        assert row.status == "available"
        assert row.available_evidence == "name"

    async def test_status_is_not_demoted_when_the_queue_briefly_empties(
            self, db, owner):
        """Lidarr pulls a record from the queue before the importer writes it."""
        request = make_request(db, owner, MBID_WAITING, status="downloading")
        lidarr = FakeLidarr(albums={MBID_WAITING: album_row(MBID_WAITING,
                                                            album_id=7)})

        await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()
        assert db.get(Request, request.id).status == "downloading"

    async def test_last_synced_at_is_written_even_without_a_transition(
            self, db, owner):
        request = make_request(db, owner, MBID_WAITING)
        lidarr = FakeLidarr(albums={MBID_WAITING: album_row(MBID_WAITING,
                                                            album_id=7)})
        await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()
        assert db.get(Request, request.id).last_synced_at == NOW

    async def test_lidarr_ids_are_learned_from_the_album_row(self, db, owner):
        request = make_request(db, owner, MBID_WAITING)
        lidarr = FakeLidarr(albums={MBID_WAITING: album_row(MBID_WAITING,
                                                            album_id=7)})
        await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()
        row = db.get(Request, request.id)
        assert (row.lidarr_album_id, row.lidarr_artist_id) == (7, 505)

    async def test_queue_failure_degrades_instead_of_aborting(self, db, owner):
        request = make_request(db, owner, MBID_WAITING)
        lidarr = FakeLidarr(albums={MBID_WAITING: album_row(MBID_WAITING,
                                                            album_id=7)})
        lidarr.get_queue = AsyncMock(side_effect=RuntimeError("lidarr is down"))

        result = await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()
        # The album row was still readable, so the cycle still did its job.
        assert result["total"] == 1
        assert db.get(Request, request.id).last_synced_at == NOW

    async def test_one_broken_row_does_not_stop_the_cycle(self, db, owner):
        broken = make_request(db, owner, MBID_VANISHED)
        good = make_request(db, owner, MBID_QUEUED)

        class FlakyLidarr(FakeLidarr):
            async def find_album_by_release_group(self, mbid):
                if mbid == MBID_VANISHED:
                    raise RuntimeError("boom")
                return self.albums.get(mbid)

        lidarr = FlakyLidarr(albums={MBID_QUEUED: album_row(MBID_QUEUED,
                                                            album_id=3)},
                             queue=[{"albumId": 3, "status": "downloading"}])

        await rs.reconcile_open_requests(db, lidarr=lidarr, now=NOW)
        db.expire_all()
        assert db.get(Request, broken.id).status == "approved"  # untouched
        assert db.get(Request, good.id).status == "downloading"

    async def test_denied_and_cancelled_are_never_reconciled(self, db, owner):
        denied = make_request(db, owner, MBID_OWNED, status="denied")
        db.add(Album(id="nav-4", name="An Album", artist_name="An Artist",
                     release_group_mbid=MBID_OWNED))
        db.commit()

        await rs.reconcile_open_requests(db, lidarr=FakeLidarr(), now=NOW)
        db.expire_all()
        assert db.get(Request, denied.id).status == "denied"

    async def test_failed_rows_are_still_watched(self, db, owner):
        """Lidarr's own auto-search can deliver after our attempt failed."""
        request = make_request(db, owner, MBID_OWNED, status="failed")
        db.add(Album(id="nav-5", name="An Album", artist_name="An Artist",
                     release_group_mbid=MBID_OWNED))
        db.commit()

        await rs.reconcile_open_requests(db, lidarr=FakeLidarr(), now=NOW)
        db.expire_all()
        assert db.get(Request, request.id).status == "available"


# ── Artist rollup ───────────────────────────────────────────────────────────


class TestParentRollup:
    def _artist_with_children(self, db, owner, statuses):
        parent = make_request(db, owner, None, status="downloading",
                              kind="artist", title=None, artist="Daft Punk")
        for index, status in enumerate(statuses):
            child = Request(
                user_id=owner.id, kind="album",
                release_group_mbid=f"bbbbbbbb-0000-0000-0000-{index:012d}",
                artist_name="Daft Punk", album_title=f"Album {index}",
                status=status, parent_request_id=parent.id,
                requested_at=NOW - timedelta(hours=6),
            )
            db.add(child)
        db.commit()
        return parent

    def test_all_children_available_makes_the_parent_available(self, db, owner):
        parent = self._artist_with_children(db, owner, ["available", "available"])
        rs._roll_up_parents(db, NOW)
        db.expire_all()
        assert db.get(Request, parent.id).status == "available"

    def test_in_flight_child_makes_the_parent_downloading(self, db, owner):
        parent = self._artist_with_children(
            db, owner, ["available", "downloading"])
        parent.status = "approved"
        db.commit()
        rs._roll_up_parents(db, NOW)
        db.expire_all()
        assert db.get(Request, parent.id).status == "downloading"

    def test_partial_children_do_not_claim_the_parent_is_available(
            self, db, owner):
        parent = self._artist_with_children(
            db, owner, ["available", "failed"])
        rs._roll_up_parents(db, NOW)
        db.expire_all()
        assert db.get(Request, parent.id).status == "downloading"


# ── Parsing helpers ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_utc_normalises_offset_aware_timestamps(self):
        assert rs.utc("2026-11-28T12:00:00Z") == NOW
        assert rs.utc("2026-11-28T07:00:00-05:00") == NOW

    def test_utc_tolerates_garbage(self):
        assert rs.utc(None) is None
        assert rs.utc("not a date") is None

    def test_queue_index_ignores_records_without_an_album(self):
        assert rs.queue_album_ids([
            {"albumId": 5}, {"albumId": None}, {"trackId": 9},
        ]) == {5}

    def test_failures_index_keeps_the_latest_per_album(self):
        older = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        newer = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        index = rs.failures_by_album([
            {"albumId": 2, "eventType": "downloadFailed", "date": older},
            {"albumId": 2, "eventType": "importFailed", "date": newer},
            {"albumId": 3, "eventType": "downloadImported", "date": newer},
        ])
        assert index[2] == (NOW - timedelta(hours=1), "importFailed")
        assert 3 not in index

    def test_partial_import_events_are_not_failures(self):
        """`albumImportIncomplete` fires on every partial import; the album's
        own statistics describe that case better than the event stream."""
        index = rs.failures_by_album([
            {"albumId": 4, "eventType": "albumImportIncomplete",
             "date": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
        ])
        assert index == {}
