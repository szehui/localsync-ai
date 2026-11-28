"""Reconciler → notifier wiring.

The reconciler is where announcements are *not* obvious: it runs every five
minutes over every open request, so "notify on transition" without care becomes
"notify every cycle", and a thirty-album discography becomes thirty messages.

What is pinned here:

* an `available` transition produces exactly one announcement, even across two
  cycles and even when the announcement transport is down;
* a child album of an artist request is silent; its parent announces once the
  whole discography has landed;
* a status is not re-announced by the next cycle.
"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, NotificationConfig, Request, User
from app.services import notifier
from app.services import request_sync as rs

RG = "11111111-1111-1111-1111-111111111111"
WEBHOOK = "https://discord.com/api/webhooks/1/token"


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
    user = User(username="sze", display_name="Sze", navidrome_url="http://nav",
                navidrome_password="x", is_owner=True)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def configured(db):
    db.add(NotificationConfig(id=1, webhook_url=WEBHOOK, enabled=True,
                              events=json.dumps(list(notifier.EVENTS))))
    db.commit()


def lidarr_with(albums: dict, *, queue=(), history=()):
    """A Lidarr stub: ``{release_group_mbid: album payload}``."""
    service = MagicMock()

    async def find_album(rg_mbid):
        return albums.get(rg_mbid)

    service.find_album_by_release_group = AsyncMock(side_effect=find_album)
    service.get_queue = AsyncMock(return_value=list(queue))
    service.get_history = AsyncMock(return_value=list(history))
    return service


def album_payload(album_id: int, *, percent: int = 100):
    return {"id": album_id, "artistId": 7, "releaseDate": "2013-05-17",
            "statistics": {"percentOfTracks": percent, "trackCount": 10}}


def library_index(owned: bool = True):
    """An ownership index that says what the test wants it to say."""
    index = MagicMock()
    index.lookup.return_value = (owned, "mbid" if owned else None)
    return index


def single(db, owner, status="downloading", **kwargs):
    row = Request(user_id=owner.id, kind="album", release_group_mbid=RG,
                  artist_name="Daft Punk", album_title="RAM", status=status,
                  **kwargs)
    db.add(row)
    db.commit()
    return row


class TestAnnouncements:
    async def test_available_is_announced_once(self, db, owner, configured):
        row = single(db, owner)
        lidarr = lidarr_with({RG: album_payload(11)})
        posted = AsyncMock(return_value=True)

        with patch.object(notifier, "post_webhook", posted):
            first = await rs.reconcile_open_requests(
                db, lidarr=lidarr, index=library_index())
            second = await rs.reconcile_open_requests(
                db, lidarr=lidarr, index=library_index())

        assert first["changed"] == 1
        assert second["changed"] == 0
        assert posted.await_count == 1

    async def test_second_cycle_does_not_re_announce_a_failure(self, db, owner,
                                                               configured):
        row = single(db, owner, status="approved", requested_at=datetime(2026, 1, 1))
        db.commit()
        # In Lidarr, no files, no queue, and a failure event after the ask.
        lidarr = lidarr_with(
            {RG: album_payload(11, percent=0)},
            history=[{"albumId": 11, "eventType": "downloadFailed",
                      "date": "2026-01-02T00:00:00Z"}],
        )
        posted = AsyncMock(return_value=True)

        with patch.object(notifier, "post_webhook", posted):
            await rs.reconcile_open_requests(db, lidarr=lidarr,
                                             index=library_index(owned=False))
            await rs.reconcile_open_requests(db, lidarr=lidarr,
                                             index=library_index(owned=False))

        db.refresh(row)
        assert row.status == "failed"
        assert posted.await_count == 1

    async def test_transport_failure_does_not_repeat_the_next_cycle(self, db, owner,
                                                                   configured):
        """A dead webhook loses one message, it does not queue a backlog."""
        row = single(db, owner)
        lidarr = lidarr_with({RG: album_payload(11)})
        posted = AsyncMock(side_effect=Exception("connection refused"))

        with patch.object(notifier, "post_webhook", posted):
            await rs.reconcile_open_requests(db, lidarr=lidarr, index=library_index())
            await rs.reconcile_open_requests(db, lidarr=lidarr, index=library_index())

        assert posted.await_count == 1

    async def test_no_config_means_no_announcement(self, db, owner):
        row = single(db, owner)
        lidarr = lidarr_with({RG: album_payload(11)})
        posted = AsyncMock(return_value=True)

        with patch.object(notifier, "post_webhook", posted):
            await rs.reconcile_open_requests(db, lidarr=lidarr, index=library_index())

        db.refresh(row)
        assert row.status == "available"
        posted.assert_not_awaited()

    async def test_child_album_is_quiet_until_the_parent_completes(self, db, owner,
                                                                   configured):
        """One message per album would be thirty messages for one ask."""
        parent = Request(user_id=owner.id, kind="artist", artist_mbid="artist-1",
                         artist_name="Daft Punk", scope="studio", status="downloading")
        db.add(parent)
        db.commit()

        child1 = Request(user_id=owner.id, kind="album", release_group_mbid=RG,
                         artist_name="Daft Punk", album_title="Homework",
                         status="downloading", parent_request_id=parent.id)
        child2 = Request(user_id=owner.id, kind="album",
                         release_group_mbid="22222222-2222-2222-2222-222222222222",
                         artist_name="Daft Punk", album_title="Discovery",
                         status="downloading", parent_request_id=parent.id)
        db.add_all([child1, child2])
        db.commit()

        # Only the first child is in the library this cycle; the second is still
        # downloading, so the discography is incomplete and nothing is announced.
        lidarr = lidarr_with(
            {RG: album_payload(11),
             "22222222-2222-2222-2222-222222222222": album_payload(12, percent=0)},
            queue=[{"albumId": 12}],
        )
        index = MagicMock()

        def lookup(rg_mbid, artist, title, song_count=None):
            return (rg_mbid == RG, "mbid" if rg_mbid == RG else None)

        index.lookup.side_effect = lookup
        posted = AsyncMock(return_value=True)

        with patch.object(notifier, "post_webhook", posted):
            await rs.reconcile_open_requests(db, lidarr=lidarr, index=index)

        db.refresh(child1)
        assert child1.status == "available"
        # Nothing announced yet: the discography is not complete.
        posted.assert_not_awaited()

        # Now the second album lands; the parent rolls up and announces once.
        lidarr2 = lidarr_with({
            RG: album_payload(11),
            "22222222-2222-2222-2222-222222222222": album_payload(12),
        })
        index2 = MagicMock()
        index2.lookup.return_value = (True, "mbid")
        with patch.object(notifier, "post_webhook", posted):
            await rs.reconcile_open_requests(db, lidarr=lidarr2, index=index2)

        db.refresh(child2)
        db.refresh(parent)
        assert child2.status == "available"
        assert parent.status == "available"
        assert posted.await_count == 1
        payload = posted.await_args.args[1]
        embed = payload["embeds"][0]
        assert "Daft Punk — studio" in embed["title"]


class TestFoundingEvent:
    async def test_created_notification_stamps_the_status(self, db, owner, configured):
        row = single(db, owner, status="pending")
        posted = AsyncMock(return_value=True)
        with patch.object(notifier, "post_webhook", posted):
            await notifier.notify(db, "request.created", row, actor="Sze")
        db.refresh(row)
        assert row.notified_status == "pending"
        assert posted.await_count == 1


class TestArtistParentIsNotFailed:
    async def test_parent_with_no_album_is_left_to_the_rollup(self, db, owner):
        """Regression: the parent has no release-group MBID of its own.

        Before this guard, `find_album_by_release_group(None)` returned nothing,
        `decide()` read "album is no longer in Lidarr", and every artist request
        flipped to `failed` on its first reconcile cycle — while its children
        were still downloading.
        """
        parent = Request(user_id=owner.id, kind="artist", artist_mbid="artist-1",
                         artist_name="Daft Punk", scope="studio",
                         status="downloading")
        db.add(parent)
        db.commit()

        lidarr = lidarr_with({})
        await rs.reconcile_open_requests(db, lidarr=lidarr, index=library_index())

        db.refresh(parent)
        assert parent.status == "downloading"

    async def test_parent_is_not_asked_for_an_album_at_all(self, db, owner):
        parent = Request(user_id=owner.id, kind="artist", artist_mbid="artist-1",
                         artist_name="Daft Punk", scope="studio",
                         status="downloading")
        db.add(parent)
        db.commit()

        lidarr = lidarr_with({})
        await rs.reconcile_open_requests(db, lidarr=lidarr, index=library_index())

        # One lookup is a wasted Lidarr call per cycle; None is not a query.
        lidarr.find_album_by_release_group.assert_not_awaited()
