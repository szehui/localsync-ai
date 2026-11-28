"""Requests API: ask, vote, approve, deny, cancel, retry.

Every endpoint is scoped by the caller's tenant. A request belonging to someone
else answers **404**, not 403 — matching the trigger convention in this repo: the
existence of another household member's row is not the caller's business, and a
403 would confirm it.

Approval is owner-only (or the requester's own auto-approved ask). The rule lives
in ``services.requests`` so it cannot be bypassed by another caller; the router
maps its ``RequestError`` to HTTP status codes and adds nothing of its own.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.database import Request, User, get_db
from app.models.schemas import (
    ArtistRequestPlan,
    RequestCreate,
    RequestListResponse,
    RequestResponse,
    RequestVoteResponse,
)
from app.routers.auth import get_current_user
from app.services import requests as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/requests", tags=["requests"])


def _error(exc: svc.RequestError) -> HTTPException:
    return HTTPException(status_code=exc.status,
                         detail={"code": exc.code, "message": exc.message})


def _display_name(db: Session, user_id: Optional[int]) -> Optional[str]:
    if user_id is None:
        return None
    row = db.get(User, user_id)
    if row is None:
        return None
    return row.display_name or row.username


def _to_response(db: Session, request: Request, *, votes: Optional[int] = None,
                 include_children: bool = True) -> RequestResponse:
    """Render one request with everything the list needs, no upstream calls."""
    return RequestResponse(
        id=request.id,
        kind=request.kind,
        status=request.status,
        release_group_mbid=request.release_group_mbid,
        artist_mbid=request.artist_mbid,
        artist_name=request.artist_name,
        album_title=request.album_title,
        year=request.year,
        primary_type=request.primary_type,
        cover_url=request.cover_url,
        scope=request.scope,
        note=request.note,
        requested_by=_display_name(db, request.user_id),
        requested_at=request.requested_at,
        decided_at=request.decided_at,
        decided_by=_display_name(db, request.decided_by),
        vote_count=votes if votes is not None else svc.vote_count(db, request.id),
        last_error=request.last_error,
        lidarr_album_id=request.lidarr_album_id,
        rollup=svc.rollup(db, request) if include_children else {},
    )


def _get_owned_request(db: Session, request_id: int, user: dict) -> Request:
    """Load a request the caller is allowed to see, else 404.

    Owners see everything; everyone else only their own asks (including the
    children of their own discography requests).
    """
    request = db.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    caller = db.get(User, user["id"])
    if caller is None:
        raise HTTPException(status_code=401, detail="Unknown user")
    if caller.is_owner:
        return request
    if request.user_id == caller.id:
        return request
    # A child album of someone else's artist request is that person's business.
    if request.parent_request_id is not None:
        parent = db.get(Request, request.parent_request_id)
        if parent is not None and parent.user_id == caller.id:
            return request
    raise HTTPException(status_code=404, detail="Request not found")


@router.post("", response_model=RequestResponse, status_code=201)
async def create_request(
    payload: RequestCreate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask for an album, or a slice of an artist's discography.

    The response is the *existing* request when the album is already requested,
    with the caller's vote attached — the UI shows "already requested by X" and a
    vote count rather than pretending a new request was created.
    """
    try:
        if payload.kind == "artist":
            if not payload.artist_mbid:
                raise HTTPException(status_code=400,
                                    detail="artist_mbid is required for an artist request")
            result = await svc.create_artist_request(
                db, user, payload.artist_mbid,
                scope=payload.scope or "studio", note=payload.note)
        else:
            if not payload.release_group_mbid:
                raise HTTPException(
                    status_code=400,
                    detail="release_group_mbid is required for an album request")
            result = await svc.create_album_request(
                db, user, payload.release_group_mbid, note=payload.note)
    except svc.RequestError as exc:
        raise _error(exc)

    request = result["request"]
    response = _to_response(db, request)
    # 201 only when a row was actually created; a vote on an existing request is
    # a successful no-op, not a new resource.
    return response


@router.get("", response_model=RequestListResponse)
async def list_requests(
    scope: str = Query("mine", pattern="^(mine|all)$"),
    status: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's requests, or the whole household's when they are the owner."""
    caller = db.get(User, user["id"])
    if caller is None:
        raise HTTPException(status_code=401, detail="Unknown user")

    query = db.query(Request)
    if scope == "all":
        if not caller.is_owner:
            raise HTTPException(status_code=403,
                                detail="Only the owner can see every request")
    else:
        query = query.filter(Request.user_id == caller.id)

    if status:
        query = query.filter(Request.status == status)

    # Artist parents last: their children are the detail, and a mixed list reads
    # best newest-first.
    rows = query.order_by(Request.requested_at.desc(), Request.id.desc()).all()
    counts = svc.vote_counts(db, [r.id for r in rows])
    return RequestListResponse(
        requests=[_to_response(db, r, votes=counts.get(r.id, 0)) for r in rows],
        total=len(rows),
    )


@router.get("/plan", response_model=ArtistRequestPlan)
async def plan_artist_request(
    artist_mbid: str = Query(...),
    scope: str = Query("studio", pattern="^(studio|studio_eps|everything)$"),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """What a discography request would ask for, before anything is created.

    Exists so the confirm dialog can state a real count ("17 albums; 3 already in
    your library") instead of the user discovering it after clicking.
    """
    try:
        return await svc.plan_artist_request(db, artist_mbid, scope=scope)
    except svc.RequestError as exc:
        raise _error(exc)


@router.post("/sync")
async def sync_requests(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reconcile every in-flight request now, instead of waiting for the cycle.

    Owner-only: it is one shared query against Lidarr, and a household member
    hammering it is throttling for everyone else's downloads. Declared before
    the ``/{request_id}`` routes so "sync" is never parsed as an id.
    """
    caller = db.get(User, user["id"])
    if caller is None:
        raise HTTPException(status_code=401, detail="Unknown user")
    if not caller.is_owner:
        raise HTTPException(status_code=403,
                            detail="Only the owner can trigger a sync")

    from app.services.request_sync import reconcile_open_requests

    return await reconcile_open_requests(db)


@router.get("/{request_id}", response_model=RequestResponse)
async def get_request(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request = _get_owned_request(db, request_id, user)
    response = _to_response(db, request)
    if request.kind == "artist":
        children = (
            db.query(Request)
            .filter(Request.parent_request_id == request.id)
            .order_by(Request.album_title)
            .all()
        )
        counts = svc.vote_counts(db, [c.id for c in children])
        response.children = [
            _to_response(db, c, votes=counts.get(c.id, 0), include_children=False)
            for c in children
        ]
    return response


@router.post("/{request_id}/vote", response_model=RequestVoteResponse)
async def vote(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add the caller's vote. Voting twice is a successful no-op."""
    request = db.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if not svc.is_open(request):
        raise HTTPException(status_code=409,
                            detail=f"Request is already {request.status}")

    added = svc.add_vote(db, request, user)
    return RequestVoteResponse(
        request_id=request.id,
        voted=True,
        added=added,
        vote_count=svc.vote_count(db, request.id),
    )


@router.post("/{request_id}/approve", response_model=RequestResponse)
async def approve(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request = _get_owned_request(db, request_id, user)
    try:
        request = await svc.approve(db, request, user)
    except svc.RequestError as exc:
        raise _error(exc)
    return _to_response(db, request)


@router.post("/{request_id}/deny", response_model=RequestResponse)
async def deny(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    reason: Optional[str] = Query(None),
):
    request = _get_owned_request(db, request_id, user)
    try:
        request = svc.deny(db, request, user, reason=reason)
    except svc.RequestError as exc:
        raise _error(exc)
    return _to_response(db, request)


@router.post("/{request_id}/cancel", response_model=RequestResponse)
async def cancel(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request = _get_owned_request(db, request_id, user)
    try:
        request = svc.cancel(db, request, user)
    except svc.RequestError as exc:
        raise _error(exc)
    return _to_response(db, request)


@router.post("/{request_id}/retry", response_model=RequestResponse)
async def retry(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request = _get_owned_request(db, request_id, user)
    try:
        request = await svc.retry(db, request, user)
    except svc.RequestError as exc:
        raise _error(exc)
    return _to_response(db, request)
