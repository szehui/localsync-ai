"""Authentication and Navidrome connection router."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db
from app.models.schemas import NavidromeConfig, ConnectionStatus, SyncStatus
from app.services.navidrome import NavidromeClient
from app.services.sync import SyncService

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory store for current connection (persisted to DB later)
_current_client: NavidromeClient | None = None
_current_sync_service: SyncService | None = None


def get_navidrome_client() -> NavidromeClient | None:
    return _current_client


def get_sync_service() -> SyncService | None:
    return _current_sync_service


@router.post("/connect", response_model=ConnectionStatus)
async def connect(config: NavidromeConfig):
    """Test and establish connection to Navidrome."""
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
        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=400, detail=str(e))


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
        message="Syncing..." if _current_sync_service.is_syncing else "Idle",
    )


@router.post("/sync", response_model=SyncStatus)
async def trigger_sync(db: Session = Depends(get_db)):
    """Trigger a full library sync from Navidrome."""
    global _current_client, _current_sync_service
    if _current_client is None:
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")
    
    # Create sync service if not already created
    if _current_sync_service is None:
        _current_sync_service = SyncService(_current_client)
    
    if _current_sync_service.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")
    
    try:
        stats = await _current_sync_service.full_sync(db)
        message = f"Synced {stats['tracks']} tracks, {stats['albums']} albums, {stats['artists']} artists"
        logger.info(message)
        return SyncStatus(
            is_syncing=False,
            last_sync=_current_sync_service.last_sync,
            message=message,
        )
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")
