"""Authentication and Navidrome connection router."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db
from app.models.schemas import NavidromeConfig, ConnectionStatus
from app.services.navidrome import NavidromeClient

router = APIRouter()

# In-memory store for current connection (persisted to DB later)
_current_client: NavidromeClient | None = None


def get_navidrome_client() -> NavidromeClient | None:
    return _current_client


@router.post("/connect", response_model=ConnectionStatus)
async def connect(config: NavidromeConfig):
    """Test and establish connection to Navidrome."""
    global _current_client
    client = NavidromeClient(
        url=config.url,
        username=config.username,
        password=config.password,
    )
    try:
        result = await client.ping()
        version = result.get("version", "unknown")
        _current_client = client
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
