"""Notification settings: one household webhook, owner-only.

The webhook URL is a credential. It is write-only over the API: `GET` returns a
masked hint (`https://discord.com/api/webhooks/<id>/…`) and never the token, and
`PUT` accepts an omitted `webhook_url` to mean "keep the stored one". A UI that
round-tripped the GET response back into the PUT would otherwise overwrite a
working URL with its own mask — a failure that reads as "the webhook stopped
working" hours later.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import NotificationConfig, User, get_db
from app.models.schemas import (
    NotificationConfigRequest,
    NotificationConfigResponse,
    NotificationTestResponse,
)
from app.routers.auth import get_current_user
from app.services import notifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _require_owner(db: Session, user: dict) -> User:
    row = db.get(User, user["id"])
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown user")
    if not row.is_owner:
        raise HTTPException(status_code=403,
                            detail="Only the owner can configure notifications")
    return row


def _to_response(config: Optional[NotificationConfig]) -> NotificationConfigResponse:
    if config is None:
        return NotificationConfigResponse(
            configured=False, enabled=False, events=list(notifier.EVENTS),
            available_events=list(notifier.EVENTS))
    return NotificationConfigResponse(
        configured=bool(config.webhook_url),
        enabled=bool(config.enabled),
        webhook_hint=notifier.mask_webhook(config.webhook_url),
        # The stored preference, not the effective one: a paused webhook must
        # still show which events it would send when re-enabled.
        events=list(notifier.stored_events(config)),
        available_events=list(notifier.EVENTS),
        base_url=config.base_url,
    )


@router.get("", response_model=NotificationConfigResponse)
async def get_notifications(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_owner(db, user)
    return _to_response(notifier.get_config(db))


@router.put("", response_model=NotificationConfigResponse)
async def set_notifications(
    payload: NotificationConfigRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update the webhook. Owner-only."""
    row = _require_owner(db, user)

    config = notifier.get_config(db)
    if config is None:
        config = NotificationConfig(id=1, webhook_url="", enabled=False)
        db.add(config)

    if payload.webhook_url is not None:
        url = payload.webhook_url.strip()
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400,
                                detail="webhook_url must be an http(s) URL")
        config.webhook_url = url

    config.enabled = bool(payload.enabled)
    config.base_url = (payload.base_url or "").strip() or None

    if payload.events is not None:
        unknown = [name for name in payload.events if name not in notifier.EVENTS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown events: {', '.join(sorted(unknown))}")
        # Stored as a JSON array so the set can grow without a migration.
        config.events = json.dumps([n for n in notifier.EVENTS
                                    if n in set(payload.events)])
    elif not config.events:
        config.events = json.dumps(list(notifier.EVENTS))

    if config.enabled and not config.webhook_url:
        raise HTTPException(status_code=400,
                            detail="A webhook URL is required to enable notifications")

    db.commit()
    db.refresh(config)
    logger.info("notification config updated by %s (enabled=%s)",
                row.username, config.enabled)
    return _to_response(config)


@router.delete("", response_model=NotificationConfigResponse)
async def clear_notifications(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the webhook entirely."""
    _require_owner(db, user)
    config = notifier.get_config(db)
    if config is not None:
        db.delete(config)
        db.commit()
    return _to_response(None)


@router.post("/test", response_model=NotificationTestResponse)
async def test_notification(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test message to the configured webhook.

    Deliberately reports the transport result: the whole point is to tell the
    owner whether the URL works *now*, so a 404 from a deleted webhook must
    surface as a failure rather than a cheerful "sent".
    """
    _require_owner(db, user)
    config = notifier.get_config(db)
    if config is None or not config.webhook_url:
        raise HTTPException(status_code=400, detail="No webhook is configured")

    payload = {
        "embeds": [{
            "title": "localsync-ai notifications are working",
            "description": "This is a test message. Album events will appear here.",
            "color": 0x3B82F6,
        }],
    }
    sent = await notifier.post_webhook(config.webhook_url, payload)
    return NotificationTestResponse(
        sent=sent,
        detail="Test message delivered." if sent
        else "Discord refused the message — check the webhook URL.",
    )
