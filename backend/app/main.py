"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from app.models.database import init_db, engine, SessionLocal, SmartTrigger, ConnectionConfig
from app.routers import auth, library, playlists, triggers
from app.routers import youtube_import, discovery
from app.config import settings
from app.services.scheduler import init_scheduler, add_trigger_job
from app.services.lidarr import LidarrService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Scheduler setup
jobstores = {
    "default": SQLAlchemyJobStore(engine=engine)
}
scheduler = AsyncIOScheduler(jobstores=jobstores)

# Lidarr service global instance
lidarr_service: LidarrService | None = None


def set_lidarr_service(service: LidarrService | None):
    """Set the global Lidarr service instance."""
    global lidarr_service
    lidarr_service = service


def load_existing_triggers():
    """On startup, re-register scheduler jobs for all enabled triggers in the DB."""
    db = SessionLocal()
    try:
        enabled_triggers = db.query(SmartTrigger).filter(SmartTrigger.enabled == True).all()
        for trigger in enabled_triggers:
            try:
                add_trigger_job(trigger)
                logger.info(f"Loaded trigger job: {trigger.name} ({trigger.trigger_type})")
            except Exception as e:
                logger.warning(f"Failed to load trigger {trigger.id}: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, start scheduler, load existing triggers."""
    logger.info("Starting LocalSync AI...")
    init_db()
    logger.info("Database initialized")

    # Give scheduler service a reference to the running scheduler
    init_scheduler(scheduler)

    # Start scheduler
    scheduler.start()
    logger.info("Scheduler started")

    # Load existing trigger jobs from DB
    load_existing_triggers()

    # Try to restore Lidarr connection from saved config
    from app.models.database import LidarrConfig
    db = SessionLocal()
    try:
        lidarr_config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
        if lidarr_config:
            try:
                service = LidarrService(lidarr_config.url, lidarr_config.api_key)
                await service.test_connection()
                set_lidarr_service(service)
                logger.info("Auto-connected to Lidarr from saved config")
            except Exception as e:
                logger.warning(f"Failed to auto-connect to Lidarr: {e}")
    finally:
        db.close()

    yield

    # Shutdown
    scheduler.shutdown()
    logger.info("LocalSync AI shutdown complete")

app = FastAPI(
    title="LocalSync AI",
    description="Music discovery and playlist orchestration for Navidrome",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(playlists.router, prefix="/api/playlists", tags=["playlists"])
app.include_router(triggers.router, prefix="/api/triggers", tags=["triggers"])
app.include_router(youtube_import.router, prefix="/api/youtube-import", tags=["youtube-import"])
app.include_router(discovery.router, prefix="/api/discovery", tags=["discovery"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
