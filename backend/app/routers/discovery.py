"""Discovery feed router.

The feed answers "what should I buy next?", which is the inverse of the playlist
generators: candidates that fail to resolve to a local song id are the product,
not a failure.

Refresh is a background job because it is rate-limited upstream (ListenBrainz
allows 1 call/sec), so a cold run takes ~15-20s. The UI polls `GET /` and reads
`state.last_status`.
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import (
    DiscoveryOffer,
    LidarrConfig,
    ListenbrainzConfig,
    SessionLocal,
    get_db,
)
from app.services import lidarr
from app.models.schemas import (
    DiscoveryAddRequest,
    DiscoveryAddResponse,
    DiscoveryArtistGroup,
    DiscoveryCounts,
    DiscoveryFeedResponse,
    DiscoveryRefreshResponse,
    DiscoveryStateResponse,
)
from app.routers.auth import get_current_user
from app.services import discovery
from app.services.lidarr import LidarrService
from app.services.listenbrainz import ListenBrainzClient

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

STATUS_RUNNING = "running"


def _get_lidarr_service(db: Session) -> Optional[LidarrService]:
    """Lidarr service from the shared global, else from saved config."""
    import app.main as _main

    if _main.lidarr_service is not None:
        return _main.lidarr_service

    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config:
        _main.set_lidarr_service(LidarrService(config.url, config.api_key))
        return _main.lidarr_service
    return None


def _build_counts(offers: list[DiscoveryOffer]) -> DiscoveryCounts:
    new_artists = {
        discovery.normalize_artist(o.artist_name)
        for o in offers if o.bucket == discovery.NEW_ARTIST
    }
    known_artists = {
        discovery.normalize_artist(o.artist_name)
        for o in offers if o.bucket == discovery.ARTIST_KNOWN
    }
    return DiscoveryCounts(
        new_artist=len(new_artists),
        artist_known=len(known_artists),
        total_offers=len(offers),
    )


async def _run_refresh_background() -> None:
    """Background refresh with its own DB session (the request's session is gone)."""
    db = SessionLocal()
    try:
        config = db.query(ListenbrainzConfig).filter(ListenbrainzConfig.id == 1).first()
        if config is None:
            state = discovery.get_state(db)
            state.last_status = "error"
            state.last_error = (
                "ListenBrainz is not configured; add a username and token in "
                "Automation Hub"
            )
            db.commit()
            return

        client = ListenBrainzClient(
            user_token=config.user_token,
            username=config.username,
            api_base=config.api_base or "https://api.listenbrainz.org",
            labs_api_base=config.labs_api_base or "https://labs.api.listenbrainz.org",
        )
        try:
            await discovery.refresh_feed(db, client)
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Discovery background refresh failed")
        try:
            state = discovery.get_state(db)
            state.last_status = "error"
            state.last_error = str(exc)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


@router.get("/", response_model=DiscoveryFeedResponse)
async def get_feed(limit: int = discovery.DEFAULT_FEED_ARTISTS,
                   db: Session = Depends(get_db)):
    """Current discovery feed, grouped by artist."""
    offers = (
        db.query(DiscoveryOffer)
        .filter(DiscoveryOffer.status == discovery.STATUS_OFFERED)
        .order_by(DiscoveryOffer.score.desc())
        .all()
    )
    groups = discovery.build_feed(offers, limit)
    return DiscoveryFeedResponse(
        artists=[DiscoveryArtistGroup(**g) for g in groups],
        state=DiscoveryStateResponse.model_validate(discovery.get_state(db)),
        counts=_build_counts(offers),
    )


@router.post("/refresh", response_model=DiscoveryRefreshResponse, status_code=202)
async def refresh(background: BackgroundTasks, db: Session = Depends(get_db)):
    """Kick off a background refresh. Poll `GET /` for progress."""
    state = discovery.get_state(db)
    if state.last_status == STATUS_RUNNING:
        return DiscoveryRefreshResponse(
            status=STATUS_RUNNING, message="A refresh is already running")

    # Flip to running here so an immediate poll does not look idle.
    state.last_status = STATUS_RUNNING
    state.last_error = None
    db.commit()

    background.add_task(_run_refresh_background)
    return DiscoveryRefreshResponse(
        status=STATUS_RUNNING,
        message="Refresh started; this takes ~20s on a cold run",
    )


@router.post("/{offer_id}/dismiss", response_model=DiscoveryRefreshResponse)
async def dismiss(offer_id: int, db: Session = Depends(get_db)):
    """Dismiss an offer. It never resurfaces in the feed."""
    offer = db.query(DiscoveryOffer).filter(DiscoveryOffer.id == offer_id).first()
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    offer.status = discovery.STATUS_DISMISSED
    db.commit()
    return DiscoveryRefreshResponse(status="dismissed", message="Offer dismissed")


@router.post("/{offer_id}/add", response_model=DiscoveryAddResponse)
async def add_to_lidarr(offer_id: int, request: DiscoveryAddRequest,
                        db: Session = Depends(get_db)):
    """Add the offer's album to Lidarr's wanted list (single album only)."""
    offer = db.query(DiscoveryOffer).filter(DiscoveryOffer.id == offer_id).first()
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    if offer.status == discovery.STATUS_SAVED:
        return DiscoveryAddResponse(
            offer_id=offer.id,
            artist_name=offer.artist_name,
            release_name=offer.release_name,
            album_id=offer.lidarr_album_id,
            already_present=True,
            message="Already sent to Lidarr",
        )

    if not offer.artist_name or not offer.release_name:
        raise HTTPException(
            status_code=400,
            detail="This offer has no artist/album metadata to look up in Lidarr",
        )

    service = _get_lidarr_service(db)
    if service is None:
        raise HTTPException(
            status_code=400,
            detail="Lidarr is not configured; set it up under YouTube Import",
        )

    try:
        result = await service.add_single_album(
            offer.artist_name, offer.release_name, search=request.search)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        # The retry loop in LidarrService already absorbed transient 5xx, so
        # anything reaching here survived ~10s of retries.
        logger.exception("Lidarr album add failed for offer %s", offer_id)
        status = exc.response.status_code
        if status in lidarr.RETRY_STATUSES:
            detail = (
                "Lidarr is busy indexing MusicBrainz right now "
                f"(HTTP {status} after retries). Wait a few seconds and try again."
            )
        elif status == 401 or status == 403:
            detail = "Lidarr rejected the API key. Re-check the Lidarr config."
        else:
            detail = f"Lidarr returned HTTP {status}. Check the Lidarr logs."
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        logger.exception("Lidarr album add failed for offer %s", offer_id)
        raise HTTPException(
            status_code=502,
            detail="Could not reach Lidarr. Is it running?",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Lidarr album add failed for offer %s", offer_id)
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error talking to Lidarr: {type(exc).__name__}",
        ) from exc

    offer.status = discovery.STATUS_SAVED
    offer.lidarr_album_id = result.get("album_id")
    offer.lidarr_artist_name = result.get("artist_name")
    db.commit()

    return DiscoveryAddResponse(
        offer_id=offer.id,
        artist_name=result.get("artist_name") or offer.artist_name,
        release_name=result.get("release_name") or offer.release_name,
        album_id=result.get("album_id"),
        already_present=bool(result.get("already_present")),
        albums_unmonitored=result.get("albums_unmonitored") or [],
        message=(
            "Already in Lidarr" if result.get("already_present")
            else "Added to Lidarr wanted list"
        ),
    )
