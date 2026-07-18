"""Authentication and Navidrome connection router."""
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db, ConnectionConfig, SessionLocal
from app.models.schemas import NavidromeConfig, ConnectionStatus, SyncStatus
from app.services.navidrome import NavidromeClient
from app.services.sync import SyncService

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory store for current connection
_current_client: NavidromeClient | None = None
_current_sync_service: SyncService | None = None


def get_navidrome_client() -> NavidromeClient | None:
    return _current_client


def get_sync_service() -> SyncService | None:
    return _current_sync_service


async def _run_sync_in_background():
    """Run full sync in background, creating its own DB session."""
    global _current_sync_service
    if _current_sync_service is None or _current_sync_service.is_syncing:
        return
    db = None
    try:
        db = SessionLocal()
        await _current_sync_service.full_sync(db)
    except Exception as e:
        logger.error(f"Background sync failed: {e}")
    finally:
        if db is not None:
            db.close()


async def _auto_connect(db: Session) -> None:
    """Attempt to restore Navidrome connection from saved config."""
    global _current_client, _current_sync_service
    config = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
    if config is None:
        return
    try:
        client = NavidromeClient(
            url=config.url,
            username=config.username,
            password=config.password,
        )
        await client.ping()
        _current_client = client
        _current_sync_service = SyncService(client)
        logger.info("Auto-connected to Navidrome from saved config")
    except Exception as e:
        logger.warning(f"Failed to auto-connect from saved config: {e}")


@router.post("/connect", response_model=ConnectionStatus)
async def connect(config: NavidromeConfig, db: Session = Depends(get_db)):
    """Test and establish connection to Navidrome. Saves config persistently."""
    global _current_client, _current_sync_service
    # Close any previous client
    if _current_client:
        await _current_client.close()
    client = NavidromeClient(
        url=config.url,
        username=config.username,
        password=config.password,
    )
    try:
        result = await client.ping()
        version = result.get("version", "unknown")
        _current_client = client
        _current_sync_service = SyncService(client)

        # Persist config to DB
        conn = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
        if conn:
            conn.url = config.url
            conn.username = config.username
            conn.password = config.password
        else:
            db.add(ConnectionConfig(
                id=1,
                url=config.url,
                username=config.username,
                password=config.password,
            ))
        db.commit()

        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception as e:
        await client.close()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disconnect", response_model=ConnectionStatus)
async def disconnect(db: Session = Depends(get_db)):
    """Disconnect from Navidrome and clear saved config."""
    global _current_client, _current_sync_service
    if _current_client:
        await _current_client.close()
        _current_client = None
        _current_sync_service = None
    db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).delete()
    db.commit()
    return ConnectionStatus(
        connected=False,
        message="Disconnected",
    )


@router.get("/status", response_model=ConnectionStatus)
async def connection_status():
    """Check current Navidrome connection status."""
    global _current_client
    if _current_client is None:
        return ConnectionStatus(
            connected=False,
            message="Not configured",
        )
    try:
        result = await _current_client.ping()
        version = result.get("version", "unknown")
        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception:
        return ConnectionStatus(
            connected=False,
            message="Connection lost",
        )


@router.get("/sync-status", response_model=SyncStatus)
async def sync_status():
    """Check current sync status."""
    global _current_sync_service
    if _current_sync_service is None:
        return SyncStatus(
            is_syncing=False,
            last_sync=None,
            message="Not connected",
        )
    return SyncStatus(
        is_syncing=_current_sync_service.is_syncing,
        last_sync=_current_sync_service.last_sync,
        track_count=_current_sync_service.last_stats.get("tracks", 0) if _current_sync_service.last_stats else 0,
        album_count=_current_sync_service.last_stats.get("albums", 0) if _current_sync_service.last_stats else 0,
        artist_count=_current_sync_service.last_stats.get("artists", 0) if _current_sync_service.last_stats else 0,
        message="Syncing..." if _current_sync_service.is_syncing else f"Idle — last sync: {_current_sync_service.last_stats.get('tracks', 0)} tracks, {_current_sync_service.last_stats.get('albums', 0)} albums, {_current_sync_service.last_stats.get('artists', 0)} artists" if _current_sync_service.last_stats else "Idle",
    )


@router.post("/sync", response_model=SyncStatus)
async def trigger_sync(db: Session = Depends(get_db)):
    """Trigger a full library sync from Navidrome (runs in background)."""
    global _current_client, _current_sync_service
    if _current_client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    # Create sync service if not already created
    if _current_sync_service is None:
        _current_sync_service = SyncService(_current_client)

    if _current_sync_service.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")

    asyncio.create_task(_run_sync_in_background())

    return SyncStatus(
        is_syncing=True,
        last_sync=_current_sync_service.last_sync,
        message="Sync started",
    )
