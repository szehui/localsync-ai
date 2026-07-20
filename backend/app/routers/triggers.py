"""Smart Triggers router."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db, SmartTrigger
from app.models.schemas import TriggerCreate, TriggerUpdate, TriggerResponse
from app.services.scheduler import add_trigger_job, remove_trigger_job, pause_trigger_job, resume_trigger_job
from app.routers.auth import get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/", response_model=list[TriggerResponse])
async def list_triggers(db: Session = Depends(get_db)):
    """List all smart triggers."""
    triggers = db.query(SmartTrigger).all()
    return [TriggerResponse.model_validate(t) for t in triggers]


@router.post("/", response_model=TriggerResponse)
async def create_trigger(trigger: TriggerCreate, db: Session = Depends(get_db)):
    """Create a new smart trigger and register its scheduler job."""
    db_trigger = SmartTrigger(
        name=trigger.name,
        trigger_type=trigger.trigger_type,
        cron_expression=trigger.cron_expression,
        threshold=trigger.threshold,
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
    trigger_id: int, update: TriggerUpdate, db: Session = Depends(get_db)
):
    """Update a smart trigger and reschedule its job."""
    trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")

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
async def delete_trigger(trigger_id: int, db: Session = Depends(get_db)):
    """Delete a smart trigger and remove its scheduler job."""
    trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    db.delete(trigger)
    db.commit()

    # Remove scheduler job
    try:
        remove_trigger_job(trigger_id)
    except Exception:
        pass

    return {"status": "deleted"}


@router.post("/{trigger_id}/toggle")
async def toggle_trigger(trigger_id: int, db: Session = Depends(get_db)):
    """Toggle a trigger on/off and pause/resume its scheduler job."""
    trigger = db.query(SmartTrigger).filter(SmartTrigger.id == trigger_id).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
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
