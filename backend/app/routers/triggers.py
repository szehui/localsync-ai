"""Smart Triggers router.

Triggers are owned: every query is scoped to the calling user's `owner_id`, and
a trigger belonging to someone else is reported as 404 rather than 403 — a 403
would confirm that the id exists and is merely off-limits, which leaks the
existence of other tenants' rows.

The router-level `get_current_user` dependency verifies the token for every
route; each handler additionally takes `user` itself so it can scope by
`user["id"]`.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db, SmartTrigger
from app.models.schemas import TriggerCreate, TriggerUpdate, TriggerResponse
from app.services.scheduler import add_trigger_job, remove_trigger_job, pause_trigger_job, resume_trigger_job
from app.routers.auth import get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


def _owned_trigger(db: Session, trigger_id: int, owner_id: int) -> SmartTrigger:
    """Load one trigger by id **and** owner, or raise 404.

    Scoping both conditions into the query (instead of loading by id and
    comparing afterwards) means another user's row is indistinguishable from a
    row that does not exist. Cross-user access must look like a plain miss.
    """
    trigger = (
        db.query(SmartTrigger)
        .filter(SmartTrigger.id == trigger_id, SmartTrigger.owner_id == owner_id)
        .first()
    )
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    return trigger


@router.get("/", response_model=list[TriggerResponse])
async def list_triggers(
    user: dict = Depends(get_current_user), db: Session = Depends(get_db)
):
    """List the calling user's smart triggers."""
    triggers = (
        db.query(SmartTrigger)
        .filter(SmartTrigger.owner_id == user["id"])
        .all()
    )
    return [TriggerResponse.model_validate(t) for t in triggers]


@router.post("/", response_model=TriggerResponse)
async def create_trigger(
    trigger: TriggerCreate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new smart trigger owned by the caller and register its job."""
    db_trigger = SmartTrigger(
        owner_id=user["id"],
        name=trigger.name,
        trigger_type=trigger.trigger_type,
        cron_expression=trigger.cron_expression,
        threshold=trigger.threshold,
        mix_kind=trigger.mix_kind,
        playlist_name=trigger.playlist_name,
    )
    db.add(db_trigger)
    db.commit()
    db.refresh(db_trigger)

    # Register APScheduler job
    try:
        add_trigger_job(db_trigger)
    except Exception as e:
        # If scheduler isn't ready yet, that's okay — jobs get loaded on startup
        pass

    return TriggerResponse.model_validate(db_trigger)


@router.put("/{trigger_id}", response_model=TriggerResponse)
async def update_trigger(
    trigger_id: int,
    update: TriggerUpdate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update one of the caller's triggers and reschedule its job."""
    trigger = _owned_trigger(db, trigger_id, user["id"])

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(trigger, key, value)

    db.commit()
    db.refresh(trigger)

    # Re-register scheduler job with updated config
    try:
        remove_trigger_job(trigger_id)
        add_trigger_job(trigger)
    except Exception:
        pass

    return TriggerResponse.model_validate(trigger)


@router.delete("/{trigger_id}")
async def delete_trigger(
    trigger_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete one of the caller's triggers and remove its scheduler job."""
    trigger = _owned_trigger(db, trigger_id, user["id"])
    db.delete(trigger)
    db.commit()

    # Remove scheduler job
    try:
        remove_trigger_job(trigger_id)
    except Exception:
        pass

    return {"status": "deleted"}


@router.post("/{trigger_id}/toggle")
async def toggle_trigger(
    trigger_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle one of the caller's triggers and pause/resume its job."""
    trigger = _owned_trigger(db, trigger_id, user["id"])
    trigger.enabled = not trigger.enabled
    db.commit()

    # Pause or resume scheduler job
    try:
        if trigger.enabled:
            resume_trigger_job(trigger_id)
        else:
            pause_trigger_job(trigger_id)
    except Exception:
        pass

    return {"id": trigger_id, "enabled": trigger.enabled}
