"""Notifier: payload shape, the once-per-status rule, and credential hygiene.

The three things worth pinning, each of which has burned a real deployment
somewhere:

* the webhook URL must never appear in a log line or an API response — it is a
  credential, and ``mask_webhook`` is the only approved rendering;
* a status is announced *once*, because the reconciler revisits rows every five
  minutes and a failed row is watched for thirty days;
* a dead webhook must not fail the request lifecycle around it.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, NotificationConfig, Request, User
from app.services import notifier

WEBHOOK = ("https://discord.com/api/webhooks/123456789012345678/"
           "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP")


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
    return WEBHOOK


def make_request(db, user, **kwargs):
    row = Request(
        user_id=user.id,
        kind=kwargs.pop("kind", "album"),
        release_group_mbid=kwargs.pop("release_group_mbid", "rg-1"),
        artist_name=kwargs.pop("artist_name", "Daft Punk"),
        album_title=kwargs.pop("album_title", "Random Access Memories"),
        year=kwargs.pop("year", 2013),
        status=kwargs.pop("status", "available"),
        cover_url=kwargs.pop("cover_url", None),
        parent_request_id=kwargs.pop("parent_request_id", None),
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


def fake_client(status_code: int = 204):
    """An httpx client stub that records the POST it was handed."""
    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(
        status_code, request=httpx.Request("POST", WEBHOOK)))
    return client


# ── Masking ─────────────────────────────────────────────────────────────────


class TestMaskWebhook:
    def test_token_is_not_in_the_mask(self):
        masked = notifier.mask_webhook(WEBHOOK)
        assert masked is not None
        assert "abcdefghijklmnop" not in masked
        # The id stays so the owner can tell which channel it is.
        assert "123456789012345678" in masked

    def test_mask_keeps_scheme_and_host(self):
        masked = notifier.mask_webhook(WEBHOOK)
        assert masked.startswith("https://discord.com/api/webhooks/")

    def test_none_and_junk(self):
        assert notifier.mask_webhook(None) is None
        assert notifier.mask_webhook("not a url") == "…"


# ── Payload ─────────────────────────────────────────────────────────────────


class TestPayload:
    def test_album_available_embed(self, db, owner):
        row = make_request(db, owner, status="available",
                           available_evidence="mbid")
        payload = notifier.build_payload("request.available", row)
        embed = payload["embeds"][0]
        assert embed["title"] == (
            "Added to your library: Daft Punk — Random Access Memories")
        assert embed["color"] == notifier.COLORS["request.available"]
        names = {field["name"]: field["value"] for field in embed["fields"]}
        assert names["Artist"] == "Daft Punk"
        assert names["Year"] == "2013"
        assert names["Matched"] == "matched by MusicBrainz ID"

    def test_artist_request_describes_scope_not_a_title(self, db, owner):
        row = make_request(db, owner, kind="artist", album_title=None,
                           release_group_mbid=None, scope="studio_eps",
                           status="pending")
        payload = notifier.build_payload("request.created", row, actor="Sze")
        embed = payload["embeds"][0]
        assert "discography" not in embed["title"]
        assert "studio + eps" in embed["title"]
        names = {field["name"]: field["value"] for field in embed["fields"]}
        assert names["Scope"] == "studio + eps"
        assert names["Asked by"] == "Sze"

    def test_failure_carries_the_error_and_is_red(self, db, owner):
        row = make_request(db, owner, status="failed",
                           last_error="Lidarr reported downloadFailed")
        embed = notifier.build_payload("request.failed", row)["embeds"][0]
        assert embed["color"] == notifier.COLORS["request.failed"]
        names = {field["name"]: field["value"] for field in embed["fields"]}
        assert names["Error"] == "Lidarr reported downloadFailed"

    def test_long_values_are_clipped_not_rejected(self, db, owner):
        row = make_request(db, owner, status="failed", last_error="x" * 5000)
        embed = notifier.build_payload("request.failed", row)["embeds"][0]
        error = next(f["value"] for f in embed["fields"] if f["name"] == "Error")
        # Discord 400s the whole post when the payload is too large, so the
        # clip is what keeps one long Lidarr error from silencing the channel.
        assert len(error) <= notifier.MAX_ERROR
        assert error.endswith("…")

    def test_title_is_clipped(self, db, owner):
        row = make_request(db, owner, artist_name="A" * 400, album_title="B" * 400)
        embed = notifier.build_payload("request.created", row)["embeds"][0]
        assert len(embed["title"]) <= notifier.MAX_TITLE

    def test_link_is_omitted_without_a_base_url(self, db, owner):
        row = make_request(db, owner)
        embed = notifier.build_payload("request.available", row)["embeds"][0]
        assert "url" not in embed

    def test_link_points_at_the_requests_tab(self, db, owner):
        row = make_request(db, owner)
        embed = notifier.build_payload("request.available", row,
                                       base_url="https://ls.example.org/")["embeds"][0]
        assert embed["url"] == "https://ls.example.org/?tab=requests"

    def test_cover_must_be_http(self, db, owner):
        row = make_request(db, owner, cover_url="javascript:alert(1)")
        embed = notifier.build_payload("request.available", row)["embeds"][0]
        assert "thumbnail" not in embed


# ── Sending ─────────────────────────────────────────────────────────────────


class TestNotify:
    async def test_sends_and_stamps(self, db, owner, configured):
        row = make_request(db, owner, status="available")
        client = fake_client()
        sent = await notifier.notify(db, "request.available", row, client=client)
        assert sent is True
        client.post.assert_awaited_once()
        url, = client.post.await_args.args
        assert url == WEBHOOK
        assert row.notified_status == "available"

    async def test_one_announcement_per_status(self, db, owner, configured):
        row = make_request(db, owner, status="failed")
        client = fake_client()
        assert await notifier.notify(db, "request.failed", row, client=client) is True
        # The reconciler comes back every five minutes; without the stamp this
        # would be 288 messages a day for one broken album.
        assert await notifier.notify(db, "request.failed", row, client=client) is False
        assert client.post.await_count == 1

    async def test_a_new_status_is_announced_again(self, db, owner, configured):
        row = make_request(db, owner, status="failed")
        client = fake_client()
        await notifier.notify(db, "request.failed", row, client=client)
        row.status = "available"
        db.commit()
        assert await notifier.notify(db, "request.available", row, client=client) is True

    async def test_disabled_config_sends_nothing(self, db, owner):
        db.add(NotificationConfig(id=1, webhook_url=WEBHOOK, enabled=False))
        db.commit()
        row = make_request(db, owner)
        client = fake_client()
        assert await notifier.notify(db, "request.available", row, client=client) is False
        client.post.assert_not_awaited()

    async def test_no_config_sends_nothing(self, db, owner):
        row = make_request(db, owner)
        client = fake_client()
        assert await notifier.notify(db, "request.available", row, client=client) is False

    async def test_unsubscribed_event_is_skipped(self, db, owner):
        db.add(NotificationConfig(id=1, webhook_url=WEBHOOK, enabled=True,
                                  events=json.dumps(["request.available"])))
        db.commit()
        row = make_request(db, owner, status="pending")
        client = fake_client()
        assert await notifier.notify(db, "request.created", row, client=client) is False
        client.post.assert_not_awaited()

    async def test_unknown_event_is_ignored(self, db, owner, configured):
        row = make_request(db, owner)
        client = fake_client()
        assert await notifier.notify(db, "request.something", row, client=client) is False

    async def test_transport_failure_is_swallowed(self, db, owner, configured):
        row = make_request(db, owner, status="available")
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        assert await notifier.notify(db, "request.available", row, client=client) is False
        # Stamped anyway: a webhook that is down must not replay the backlog
        # when it comes back.
        assert row.notified_status == "available"

    async def test_discord_rejection_is_not_success(self, db, owner, configured):
        row = make_request(db, owner, status="available")
        client = fake_client(status_code=404)
        assert await notifier.notify(db, "request.available", row, client=client) is False

    async def test_webhook_url_never_reaches_the_log(self, db, owner, configured, caplog):
        row = make_request(db, owner, status="available")
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError(
            f"cannot connect to {WEBHOOK}"))
        with caplog.at_level("DEBUG"):
            await notifier.notify(db, "request.available", row, client=client)
        assert "abcdefghijklmnop" not in caplog.text
        assert WEBHOOK not in caplog.text


class TestSubscribedEvents:
    def test_absent_row_means_off(self):
        assert notifier.subscribed_events(None) == ()

    def test_disabled_means_off(self):
        config = NotificationConfig(webhook_url=WEBHOOK, enabled=False)
        assert notifier.subscribed_events(config) == ()

    def test_missing_events_means_all(self):
        config = NotificationConfig(webhook_url=WEBHOOK, enabled=True, events=None)
        assert notifier.subscribed_events(config) == notifier.EVENTS

    def test_malformed_json_falls_back_to_all(self, caplog):
        config = NotificationConfig(webhook_url=WEBHOOK, enabled=True,
                                    events="{not json")
        assert notifier.subscribed_events(config) == notifier.EVENTS

    def test_subset_is_honoured_in_canonical_order(self):
        config = NotificationConfig(webhook_url=WEBHOOK, enabled=True,
                                    events=json.dumps(["request.failed",
                                                       "request.created"]))
        assert notifier.subscribed_events(config) == (
            "request.created", "request.failed")


class TestBaseUrl:
    def test_forwarded_headers_win(self):
        assert notifier.base_url_from({
            "host": "internal:4535",
            "x-forwarded-host": "ls.example.org",
            "x-forwarded-proto": "https",
        }) == "https://ls.example.org"

    def test_first_value_of_a_comma_list(self):
        assert notifier.base_url_from({
            "x-forwarded-host": "ls.example.org, proxy",
            "x-forwarded-proto": "https, http",
        }) == "https://ls.example.org"

    def test_no_host_means_no_link(self):
        assert notifier.base_url_from({}) is None
