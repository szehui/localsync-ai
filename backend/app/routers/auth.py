"""Authentication and Navidrome connection router.

Endpoints:
  - POST /login   — validate Navidrome Subsonic credentials, establish connection, return JWT
  - POST /logout  — disconnect and clear session
  - GET  /me      — verify token and return current user info
  - GET  /status       — connection status (requires JWT)
  - GET  /sync-status  — sync status (requires JWT)
  - POST /sync         — trigger sync (requires JWT)

The login endpoint IS the connect step — no separate /connect needed.
"""
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.models.database import get_db, ConnectionConfig, SessionLocal
from app.models.schemas import LoginRequest, TokenResponse, UserResponse, ConnectionStatus, SyncStatus
from app.services.navidrome import NavidromeClient
from app.services.sync import SyncService
from app.services.auth import create_access_token, verify_token

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory store for current connection
_current_client: NavidromeClient | None = None
_current_sync_service: SyncService | None = None


# ── JWT auth dependency ──────────────────────────────────────────────────────

async def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """Extract and verify the JWT from the Authorization header.

    Returns a dict with 'username' and 'navidrome_url'.
    Used as a FastAPI dependency on protected routes.
    """
    if authorization is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {
        "username": payload.get("sub", "unknown"),
        "navidrome_url": payload.get("url", ""),
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

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


async def _auto_connect(db: Session) -> bool:
    """Attempt to restore Navidrome connection from saved config."""
    global _current_client, _current_sync_service
    config = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
    if config is None:
        return False
    try:
        client = NavidromeClient(
            url=config.url,
            username=config.username,
            password=config.password,
        )
        result = await client.ping()
        _current_client = client
        _current_sync_service = SyncService(client)
        logger.info("Auto-connected to Navidrome from saved config")
        return True
    except Exception as e:
        logger.warning(f"Failed to auto-connect from saved config: {e}")
        return False


async def _ensure_connected() -> bool:
    """Ensure the Navidrome client is connected, attempting auto-reconnect if not."""
    global _current_client
    if _current_client is not None:
        # Client exists — verify it's still alive
        try:
            await _current_client.ping()
            return True
        except Exception:
            _current_client = None
            _current_sync_service = None
    # Try to reconnect from saved config
    db = SessionLocal()
    try:
        return await _auto_connect(db)
    finally:
        db.close()


# ── Auth endpoints ───────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(creds: LoginRequest, db: Session = Depends(get_db)):
    """Validate Navidrome credentials and issue a JWT.

    1. Tests Navidrome connectivity with supplied credentials
    2. Saves config to DB for auto-reconnect
    3. Establishes the in-memory NavidromeClient
    4. Returns a JWT for subsequent API calls
    """
    global _current_client, _current_sync_service

    # Close any previous client
    if _current_client:
        await _current_client.close()

    client = NavidromeClient(
        url=creds.url,
        username=creds.username,
        password=creds.password,
    )
    try:
        result = await client.ping()
        _current_client = client
        _current_sync_service = SyncService(client)

        # Persist config to DB
        conn = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
        if conn:
            conn.url = creds.url
            conn.username = creds.username
            conn.password = creds.password
        else:
            db.add(ConnectionConfig(
                id=1,
                url=creds.url,
                username=creds.username,
                password=creds.password,
            ))
        db.commit()

        # Issue JWT
        token = create_access_token(data={
            "sub": creds.username,
            "url": creds.url,
        })
        return TokenResponse(access_token=token)
    except Exception as e:
        await client.close()
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")


@router.post("/logout", response_model=ConnectionStatus)
async def logout(db: Session = Depends(get_db), _user: dict = Depends(get_current_user)):
    """Disconnect from Navidrome and clear saved config."""
    global _current_client, _current_sync_service
    if _current_client:
        await _current_client.close()
        _current_client = None
        _current_sync_service = None
    db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).delete()
    db.commit()
    return ConnectionStatus(connected=False, message="Logged out")


@router.get("/me", response_model=UserResponse)
async def me(_user: dict = Depends(get_current_user)):
    """Return current authenticated user info.

    Tries to auto-reconnect Navidrome if the connection was lost (e.g. server restart).
    """
    global _current_client
    server_version = None
    if _current_client is None:
        await _ensure_connected()
    if _current_client is not None:
        try:
            result = await _current_client.ping()
            server_version = result.get("version", "unknown")
        except Exception:
            pass
    return UserResponse(
        username=_user["username"],
        navidrome_url=_user["navidrome_url"],
        server_version=server_version,
    )


# ── Connection endpoints ─────────────────────────────────────────────────────

@router.get("/status", response_model=ConnectionStatus)
async def connection_status(_user: dict = Depends(get_current_user)):
    """Check current Navidrome connection status."""
    global _current_client
    if _current_client is None:
        if await _ensure_connected():
            pass  # reconnected
        else:
            return ConnectionStatus(connected=False, message="Not configured")
    try:
        result = await _current_client.ping()
        version = result.get("version", "unknown")
        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception:
        _current_client = None
        return ConnectionStatus(connected=False, message="Connection lost")


# ── Sync endpoints ───────────────────────────────────────────────────────────

@router.get("/sync-status", response_model=SyncStatus)
async def sync_status(_user: dict = Depends(get_current_user)):
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
async def trigger_sync(db: Session = Depends(get_db), _user: dict = Depends(get_current_user)):
    """Trigger a full library sync from Navidrome (runs in background)."""
    global _current_client, _current_sync_service
    if _current_client is None:
        if not await _ensure_connected():
            raise HTTPException(status_code=400, detail="Not connected to Navidrome")

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


# ── Legacy /connect endpoint (backwards compat, protected) ───────────────────

@router.post("/connect", response_model=ConnectionStatus)
async def legacy_connect(config: LoginRequest, db: Session = Depends(get_db), _user: dict = Depends(get_current_user)):
    """Legacy endpoint: alias for login but returns connection status."""
    global _current_client, _current_sync_service
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

        conn = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
        if conn:
            conn.url = config.url
            conn.username = config.username
            conn.password = config.password
        else:
            db.add(ConnectionConfig(id=1, url=config.url, username=config.username, password=config.password))
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
async def legacy_disconnect(db: Session = Depends(get_db), _user: dict = Depends(get_current_user)):
    """Legacy endpoint: alias for logout."""
    global _current_client, _current_sync_service
    if _current_client:
        await _current_client.close()
        _current_client = None
        _current_sync_service = None
    db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).delete()
    db.commit()
    return ConnectionStatus(connected=False, message="Disconnected")
