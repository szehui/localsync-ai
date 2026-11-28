"""Outbound notifications: a Discord webhook per household, one embed per event.

Three rules shape this module, and each one is a bug that is easy to ship:

**The webhook URL is a credential.** Anyone holding it can post to the channel.
It is therefore never logged — not in a warning, not in an exception traceback,
not in the config echo — and ``mask_webhook`` is the only approved way to put it
in a string. An exception raised by httpx carries the request URL, which is why
the log lines here name the event and the request, never the URL.

**A notification must never fail the thing it describes.** Approval, import and
status reconciliation all succeed or fail on their own evidence; a webhook that
times out is decoration. Every entry point here swallows and logs.

**One announcement per status.** The reconciler walks every in-flight request
every five minutes and keeps watching ``failed`` rows for thirty days, so
"notify on transition" without bookkeeping becomes "notify every cycle".
``Request.notified_status`` is stamped when a send is *attempted*: a dead
webhook then loses one message rather than repeating it 288 times a day, and a
retry clears the stamp so a second failure can be announced.

Event names match the plan's: ``request.created``, ``request.available``,
``request.failed``.
"""
import json
import logging
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from app.models.database import NotificationConfig, Request

logger = logging.getLogger(__name__)

# Every event this app can emit. Kept as a tuple so the API and the UI offer the
# same list, and so an unknown event name in the database is simply inert.
EVENTS = ("request.created", "request.available", "request.failed")

DEFAULT_EVENTS = EVENTS

# A webhook is a courtesy ping, not a transaction. Five seconds is enough for
# Discord and short enough that a blackholed request cannot stall a cycle.
WEBHOOK_TIMEOUT = 5.0

# Discord's embed colours, as its API wants them: an integer, not a hex string.
COLORS = {
    "request.created": 0x3B82F6,     # blue — something was asked for
    "request.available": 0x22C55E,   # green — it is in the library
    "request.failed": 0xEF4444,      # red — it broke
}

# Discord rejects a post whose total embed payload exceeds 6000 characters, and
# silently truncates nothing — it 400s. Titles and error strings come from
# MusicBrainz and Lidarr, so both are clipped rather than trusted.
MAX_TITLE = 200
MAX_ERROR = 500


# ── Config ──────────────────────────────────────────────────────────────────


def get_config(db: Session) -> Optional[NotificationConfig]:
    return db.query(NotificationConfig).filter(NotificationConfig.id == 1).first()


def mask_webhook(url: Optional[str]) -> Optional[str]:
    """A safe rendering of a webhook URL for logs and API responses.

    Keeps the host and every path segment but the last, so the owner can tell
    *which* channel is configured, and drops the tail — which is the secret for
    Discord (``/api/webhooks/<id>/<token>``), Slack (``/services/T/B/<token>``)
    and every other provider that follows the same convention.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "…"
    segments = [s for s in parts.path.split("/") if s]
    head = "/".join(segments[:-1]) if len(segments) > 1 else ""
    base = f"{parts.scheme}://{parts.netloc}"
    return f"{base}/{head}/…" if head else f"{base}/…"


def stored_events(config: Optional[NotificationConfig]) -> tuple[str, ...]:
    """The events saved in the row, ignoring whether sending is paused.

    Used by the settings API: the UI must show the *preference* even while
    notifications are switched off, otherwise re-enabling them silently comes
    back with nothing ticked and the household gets no notifications at all.
    """
    if config is None:
        return EVENTS
    raw = (config.events or "").strip()
    if not raw:
        return EVENTS
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        # A malformed row must not silence the feature silently; default to all
        # events and say so, because the alternative is "configured, never fires".
        logger.warning("notification config events are malformed; assuming all")
        return EVENTS
    if not isinstance(stored, list):
        return EVENTS
    wanted = {str(name) for name in stored}
    return tuple(name for name in EVENTS if name in wanted)


def subscribed_events(config: Optional[NotificationConfig]) -> tuple[str, ...]:
    """Which events this household wants to receive right now."""
    if config is None or not config.enabled:
        return ()
    return stored_events(config)


# ── Payload ─────────────────────────────────────────────────────────────────


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(request: Request) -> str:
    """Human name for a request: an album, or an artist's discography."""
    if request.kind == "artist":
        artist = request.artist_name or "Unknown artist"
        scope = (request.scope or "studio").replace("_", " + ")
        return f"{artist} — {scope}"
    title = request.album_title or "Untitled"
    return f"{request.artist_name} — {title}" if request.artist_name else title


def build_payload(event: str, request: Request, *,
                  actor: Optional[str] = None,
                  base_url: Optional[str] = None) -> dict:
    """The Discord message for one event. Pure function: no DB, no network.

    ``base_url`` is the app's own address, used to link back to the request.
    When it is absent the embed simply has no clickable title — better than a
    link to ``http://localhost`` that works for nobody but the server.
    """
    headers = {
        "request.created": "New request",
        "request.available": "Added to your library",
        "request.failed": "Request failed",
    }
    title = headers.get(event, event)
    subject = describe(request)

    fields: list[dict] = []
    if request.artist_name and request.kind == "album":
        fields.append({"name": "Artist", "value": _clip(request.artist_name, 256)
                       or "—", "inline": True})
    if request.year:
        fields.append({"name": "Year", "value": str(request.year), "inline": True})
    if request.kind == "artist" and request.scope:
        fields.append({"name": "Scope", "value": request.scope.replace("_", " + "),
                       "inline": True})
    if actor:
        fields.append({"name": "Asked by", "value": _clip(actor, 256) or "—",
                       "inline": True})
    if request.status == "available" and request.available_evidence:
        how = "matched by MusicBrainz ID" if request.available_evidence == "mbid" \
            else "matched by artist and title"
        fields.append({"name": "Matched", "value": how, "inline": True})
    if event == "request.failed" and request.last_error:
        fields.append({"name": "Error", "value": _clip(request.last_error, MAX_ERROR)
                       or "—", "inline": False})

    embed: dict = {
        "title": _clip(f"{title}: {subject}", MAX_TITLE),
        "color": COLORS.get(event, 0x6B7280),
        "fields": fields,
    }
    if request.cover_url and request.cover_url.startswith(("http://", "https://")):
        embed["thumbnail"] = {"url": request.cover_url}
    if base_url:
        embed["url"] = f"{base_url.rstrip('/')}/?tab=requests"

    return {"embeds": [embed]}


# ── Sending ─────────────────────────────────────────────────────────────────


async def post_webhook(url: str, payload: dict, *,
                       client: Optional[httpx.AsyncClient] = None) -> bool:
    """POST to the webhook. Returns success; never raises on a transport error.

    ``client`` is injectable so tests can assert on the payload without a socket.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)
    try:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            # Discord answers 404 for a deleted webhook and 429 when rate
            # limited. Neither is the caller's problem, but both are worth a
            # line in the log — without the URL, which is a credential.
            logger.warning("webhook rejected the notification: HTTP %s",
                           response.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — a webhook is decoration
        logger.warning("webhook delivery failed (%s)", type(exc).__name__)
        return False
    finally:
        if owns_client:
            await client.aclose()


async def notify(
    db: Session,
    event: str,
    request: Request,
    *,
    actor: Optional[str] = None,
    base_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    commit: bool = True,
) -> bool:
    """Announce one event about one request, at most once per status.

    Returns True when a notification was actually sent. Every refusal — not
    configured, not subscribed, already announced, transport failure — returns
    False without raising, because the caller is a request lifecycle step whose
    success must not depend on a chat message.
    """
    if event not in EVENTS:
        logger.debug("ignoring unknown notification event %r", event)
        return False

    config = get_config(db)
    if config is None or not config.webhook_url:
        return False
    if event not in subscribed_events(config):
        return False
    if request.notified_status == request.status:
        # This exact status was already announced (or attempted).
        return False

    payload = build_payload(event, request, actor=actor,
                            base_url=base_url or config.base_url)
    sent = await post_webhook(config.webhook_url, payload, client=client)

    # Stamped on attempt, not on success: a webhook that has been deleted and
    # re-created should not replay a fortnight of history on its first cycle.
    request.notified_status = request.status
    if commit:
        db.commit()

    if sent:
        logger.info("notified %s for request %s", event, request.id)
    return sent


def base_url_from(request_headers: dict) -> Optional[str]:
    """Rebuild the app's public origin from a request's headers, if we can.

    Used so the embed links to a page the recipient can actually open. Behind a
    reverse proxy the forwarded headers are the only truthful source.
    """
    host = request_headers.get("x-forwarded-host") or request_headers.get("host")
    if not host:
        return None
    proto = request_headers.get("x-forwarded-proto") or "http"
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


async def notify_many(db: Session, event: str, requests: Iterable[Request], *,
                      client: Optional[httpx.AsyncClient] = None) -> int:
    """Announce the same event about several requests; returns how many landed.

    Exists for the reconciler, which can mark a whole discography available in
    one cycle and would otherwise pay a new HTTP client per album.
    """
    rows = list(requests)
    if not rows:
        return 0
    config = get_config(db)
    if config is None or not config.webhook_url or event not in subscribed_events(config):
        return 0

    sent = 0
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as shared:
        for row in rows:
            if await notify(db, event, row, client=client or shared, commit=False):
                sent += 1
    db.commit()
    return sent
