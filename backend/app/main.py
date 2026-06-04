"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from app.models.database import init_db, engine
from app.routers import auth, library, playlists, triggers
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Scheduler setup
jobstores = {
    "default": SQLAlchemyJobStore(engine=engine)
}
scheduler = AsyncIOScheduler(jobstores=jobstores)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, run full sync, start scheduler."""
    logger.info("Starting LocalSync AI...")
    init_db()
    logger.info("Database initialized")

    # Start scheduler
    scheduler.start()
    logger.info("Scheduler started")

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


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
