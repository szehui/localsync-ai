"""Authentication and Navidrome connection router.

Endpoints:
  - POST /login   — validate Navidrome Subsonic credentials, establish connection, return JWT
  - POST /logout  — drop the caller's live connection (credentials stay)
  - GET  /me      — verify token and return current user info
  - GET  /users        — household roster (no credentials in the payload)
  - DELETE /users/{id} — owner-only account removal (cascades per-user data)
  - GET  /status       — connection status (requires JWT)
  - GET  /sync-status  — sync status (requires JWT)
  - POST /sync         — trigger sync (requires JWT)
  - GET/PUT/DELETE /lastfm, /listenbrainz — per-user scrobbler credentials

The login endpoint IS the connect step — no separate /connect needed.

Every endpoint here acts on the *calling* user: live clients and sync services
live in the per-user registry in ``app.services.users``, and credentials live in
the ``users`` / ``*_user_config`` tables. There is no process-wide connection
state left, so two users pointed at different Navidrome servers cannot see each
other's library, and one user's logout cannot tear down another's sockets.
"""
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.models.database import (
    get_db,
    SessionLocal,
    GeneratedPlaylist,
    LastfmUserConfig,
    ListenbrainzUserConfig,
    SmartTrigger,
    User,
)
from app.models.schemas import LoginRequest, TokenResponse, UserResponse, ConnectionStatus, SyncStatus, LastfmConfigRequest, LastfmConfigResponse, ListenbrainzConfigRequest, ListenbrainzConfigResponse, ListenbrainzTestResponse, HouseholdUserResponse, DeleteUserResponse, UserFlagsUpdate
from app.services.navidrome import NavidromeClient
from app.services.listenbrainz import ListenBrainzClient, ListenBrainzError, UserNotFoundError
from app.services.sync import SyncService
from app.services.auth import create_access_token, verify_token
from app.services.users import (
    clear_clients_for_user,
    get_client,
    get_lastfm_config,
    get_listenbrainz_config,
    get_or_create_user,
    peek_client,
    peek_sync_service,
    set_client_for_user,
    UsernameOnOtherUrl,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── JWT auth dependency ──────────────────────────────────────────────────────

async def get_current_user(authorization: str | None = Header(default=None),
                           db: Session = Depends(get_db)) -> dict:
    """Verify the JWT and return the *tenant*, not just the login name.

    Returns a dict with 'id', 'username' and 'navidrome_url'; 'id' is what
    callers scope queries by. Tokens minted before multi-user support carry no
    'uid' and are rejected rather than silently treated as user 1 — scoping to
    the wrong tenant is worse than a 401.

    The token is checked against the database, not trusted on its own. A
    signature proves who signed in, not that the account still exists: removing
    a household member deleted their row but left their token working for the
    rest of its 24 hours, verified live against a deleted account — `/api/auth/me`
    and `/api/library/stats` both answered 200, and endpoints that join on the
    user row 500'd. A deleted user must be a 401 on the next request, and the
    username and library URL come from the row so a renamed or repointed user is
    not served stale claims.
    """
    if authorization is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    uid = payload.get("uid")
    if uid is None:
        raise HTTPException(
            status_code=401,
            detail="Token predates multi-user support — please log in again",
        )

    user = db.get(User, uid)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="This account no longer exists — please log in again",
        )
    return {
        "id": user.id,
        "username": user.username,
        "navidrome_url": user.navidrome_url or "",
    }


# ── Dependencies other routers import ────────────────────────────────────────

async def get_navidrome_client(user: dict = Depends(get_current_user)):
    """FastAPI dependency: a live Navidrome client for the calling user.

    Kept under its original name because playlists/ and youtube_import/ take it
    via ``Depends``. It resolves (and lazily reconnects) through the per-user
    registry instead of reading a module-level singleton.
    """
    db = SessionLocal()
    try:
        return await get_client(db, user["id"])
    except Exception as e:
        logger.warning("No usable Navidrome client for user %s: %s", user["id"], e)
        return None
    finally:
        db.close()


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _run_sync_in_background(user_id: int, svc) -> None:
    """Run one user's full sync, with a session of its own.

    The request session is already closed by the time this runs, and the
    scheduler jobs use their own sessions too — a background sync must never
    borrow a request-scoped Session.
    """
    db = None
    try:
        db = SessionLocal()
        await svc.full_sync(db)
    except Exception as e:
        logger.error("Background sync failed for user %s: %s", user_id, e)
    finally:
        if db is not None:
            db.close()


async def _connect(db: Session, creds: LoginRequest):
    """Ping Navidrome with these credentials and publish the live client.

    Shared by /login and the legacy /connect alias so both resolve the tenant,
    store the credentials the scheduler will replay, and register the client
    the same way. Raises the underlying ping error unchanged; callers choose
    the HTTP status (401 for login, 400 for the legacy alias).
    """
    client = NavidromeClient(
        url=creds.url,
        username=creds.username,
        password=creds.password,
    )
    try:
        result = await client.ping()
    except Exception:
        await client.close()
        raise

    user = get_or_create_user(
        db, url=creds.url, username=creds.username, password=creds.password
    )
    # A repeat login replaces this user's client; close the superseded one so
    # its sockets do not linger. Scoped to this user only.
    await clear_clients_for_user(user.id)
    set_client_for_user(user.id, client, SyncService(client, user.id))

    version = result.get("version", "unknown")
    return user, client, version


# ── Auth endpoints ───────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(creds: LoginRequest, db: Session = Depends(get_db)):
    """Validate Navidrome credentials and issue a JWT.

    1. Tests Navidrome connectivity with the supplied credentials
    2. Resolves (or creates) the tenant row and refreshes its stored password,
       which is what scheduled jobs replay
    3. Registers the live client for that tenant
    4. Returns a JWT naming the tenant in ``uid``
    """
    try:
        user, _client, _version = await _connect(db, creds)
    except UsernameOnOtherUrl as e:
        # Credentials verified against the new URL, but this username already
        # belongs to a tenant elsewhere. 409 with the reason, not a generic 401:
        # the person needs to know they must use the other address.
        logger.warning("Login refused: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")

    token = create_access_token(data={
        "sub": user.username,
        "url": user.navidrome_url,
        "uid": user.id,
    })
    return TokenResponse(access_token=token)


@router.post("/logout", response_model=ConnectionStatus)
async def logout(user: dict = Depends(get_current_user)):
    """Drop the calling user's live connection.

    Credentials are deliberately left in the database: scheduled triggers keep
    running on behalf of a logged-out user, rebuilding their client from the
    stored row (see app.services.users.build_client). Actually removing an
    account — and its credentials — is an owner-only delete-account endpoint
    added in a later task; logout must never be a destructive delete.

    Scoped to ``user["id"]``: other users keep their connections.
    """
    await clear_clients_for_user(user["id"])
    return ConnectionStatus(connected=False, message="Logged out")


@router.get("/me", response_model=UserResponse)
async def me(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current authenticated user info.

    Reads the registry without reconnecting: /me must answer even when the
    user's Navidrome is unreachable, and a missing client simply means
    ``server_version`` is unknown. Reconnection happens on the endpoints that
    actually need a client.
    """
    server_version = None
    client = peek_client(user["id"])
    if client is not None:
        try:
            result = await client.ping()
            server_version = result.get("version", "unknown")
        except Exception:
            server_version = None
    row = db.get(User, user["id"])
    return UserResponse(
        username=user["username"],
        navidrome_url=user["navidrome_url"],
        server_version=server_version,
        is_owner=bool(row.is_owner) if row else False,
        auto_approve=bool(row.auto_approve) if row else False,
        can_request=bool(row.can_request) if row else True,
    )


# ── Household roster (owner-managed accounts) ───────────────────────────────

@router.get("/users", response_model=list[HouseholdUserResponse])
async def list_users(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List every account in the household, newest secrets excluded.

    Any authenticated member may look at the roster — the UI needs it to show
    who else shares the server. What comes back is identity and state only:
    ``navidrome_password`` and the ListenBrainz token are not fields of
    ``HouseholdUserResponse``, so they cannot be echoed even by accident.
    ``listenbrainz_configured`` is a boolean derived from row existence.

    One query with an outer join rather than a per-user probe: the roster is
    small but N+1 queries on a page the UI polls is still waste.
    """
    rows = (
        db.query(User, ListenbrainzUserConfig.user_id)
        .outerjoin(ListenbrainzUserConfig, ListenbrainzUserConfig.user_id == User.id)
        .order_by(User.id)
        .all()
    )
    return [
        HouseholdUserResponse(
            id=member.id,
            username=member.username,
            display_name=member.display_name,
            is_owner=bool(member.is_owner),
            auto_approve=bool(member.auto_approve),
            can_request=bool(member.can_request),
            created_at=member.created_at,
            last_login=member.last_login,
            listenbrainz_configured=lb_user_id is not None,
        )
        for member, lb_user_id in rows
    ]


@router.patch("/users/{user_id}", response_model=HouseholdUserResponse)
async def update_user_flags(
    user_id: int,
    update: UserFlagsUpdate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change another member's request policy. Owner only.

    These two flags are the whole approval model, so they are the one thing a
    non-owner must never set for themselves — otherwise "auto approve" is
    self-service and the queue is decorative.

    The owner's own flags are left editable (an owner can stop being
    auto-approved) but ``can_request=False`` on the owner is refused: the owner
    is the only account that can approve, so locking themselves out of asking
    leaves the household unable to request anything.
    """
    caller = db.get(User, user["id"])
    if caller is None or not caller.is_owner:
        raise HTTPException(status_code=403,
                            detail="Only the owner can change request permissions")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if update.auto_approve is None and update.can_request is None:
        raise HTTPException(status_code=400,
                            detail="Provide auto_approve and/or can_request")

    if update.can_request is False and target.is_owner:
        raise HTTPException(
            status_code=400,
            detail="The owner cannot disable their own ability to request — "
                   "nobody would be left to approve",
        )

    if update.auto_approve is not None:
        target.auto_approve = update.auto_approve
    if update.can_request is not None:
        target.can_request = update.can_request
    db.commit()
    db.refresh(target)

    logger.info("Owner %s set flags on user %s: auto_approve=%s can_request=%s",
                caller.id, target.id, target.auto_approve, target.can_request)

    return HouseholdUserResponse(
        id=target.id,
        username=target.username,
        display_name=target.display_name,
        is_owner=bool(target.is_owner),
        auto_approve=bool(target.auto_approve),
        can_request=bool(target.can_request),
        created_at=target.created_at,
        last_login=target.last_login,
        listenbrainz_configured=db.get(ListenbrainzUserConfig, target.id) is not None,
    )


@router.delete("/users/{user_id}", response_model=DeleteUserResponse)
async def delete_user(
    user_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove another account from the household. Owner only.

    The owner check reads the caller's row from the database rather than the
    JWT: ``is_owner`` is a mutable DB flag, and a token minted before the flag
    changed must not carry elevated rights. This also means a deleted owner
    cannot keep acting on a stale token.

    Per-user rows (annotations, Last.fm/ListenBrainz config, the CF cache,
    discovery offers) go with the FK cascade. Two tables are the exception —
    ``generated_playlists`` and ``smart_triggers`` hold nullable ``owner_id``
    FKs with no ``ondelete`` cascade, so those links are cleared first; see
    the comments below.
    """
    caller = db.get(User, user["id"])
    if caller is None or not caller.is_owner:
        raise HTTPException(status_code=403, detail="Only the owner can remove users")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if target.is_owner:
        raise HTTPException(status_code=400, detail="Cannot remove the owner account")

    # Drop live sockets before the row disappears: a cached client still holds
    # this user's credentials in memory, and leaving it registered would answer
    # requests for an account that no longer exists.
    await clear_clients_for_user(user_id)

    # GeneratedPlaylist.owner_id is a plain nullable FK (no ON DELETE CASCADE),
    # and the rows are history: the UI keeps listing past mixes, and the
    # scheduler's retention/cooldown queries simply stop matching them. So the
    # link is cleared instead of the row being destroyed. With SQLite's
    # foreign_keys pragma on, deleting the user while a row still pointed here
    # would fail outright.
    db.query(GeneratedPlaylist).filter(
        GeneratedPlaylist.owner_id == user_id
    ).update({GeneratedPlaylist.owner_id: None}, synchronize_session=False)

    # SmartTrigger.owner_id is the same shape — nullable, no ondelete cascade —
    # so the triggers are orphaned rather than deleted. Their APScheduler jobs
    # stay registered but can no longer resolve a user, which fails harmlessly
    # in the job body instead of taking the account delete down with it.
    db.query(SmartTrigger).filter(
        SmartTrigger.owner_id == user_id
    ).update({SmartTrigger.owner_id: None}, synchronize_session=False)

    db.delete(target)
    db.commit()
    logger.info("Owner %s removed user %s", user["id"], user_id)
    return DeleteUserResponse(deleted=True, user_id=user_id)


# ── Connection endpoints ─────────────────────────────────────────────────────

@router.get("/status", response_model=ConnectionStatus)
async def connection_status(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Check the calling user's Navidrome connection status."""
    try:
        client = await get_client(db, user["id"])
        result = await client.ping()
        version = result.get("version", "unknown")
        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception as e:
        logger.info("Connection status for user %s: not usable (%s)", user["id"], e)
        return ConnectionStatus(connected=False, message="Not configured")


# ── Sync endpoints ───────────────────────────────────────────────────────────

@router.get("/sync-status", response_model=SyncStatus)
async def sync_status(user: dict = Depends(get_current_user)):
    """Check the calling user's sync status."""
    svc = peek_sync_service(user["id"])
    if svc is None:
        return SyncStatus(
            is_syncing=False,
            last_sync=None,
            message="Not connected",
        )
    stats = svc.last_stats or {}
    if svc.is_syncing:
        message = "Syncing..."
    elif svc.last_stats:
        message = (
            f"Idle — last sync: {stats.get('tracks', 0)} tracks, "
            f"{stats.get('albums', 0)} albums, {stats.get('artists', 0)} artists"
        )
    else:
        message = "Idle"
    return SyncStatus(
        is_syncing=svc.is_syncing,
        last_sync=svc.last_sync,
        track_count=stats.get("tracks", 0),
        album_count=stats.get("albums", 0),
        artist_count=stats.get("artists", 0),
        message=message,
    )


@router.post("/sync", response_model=SyncStatus)
async def trigger_sync(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Trigger a full library sync for the calling user (runs in background)."""
    existing = peek_sync_service(user["id"])
    if existing is not None and existing.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")

    try:
        client = await get_client(db, user["id"])
    except Exception as e:
        logger.info("Sync requested for user %s without a usable client: %s", user["id"], e)
        raise HTTPException(status_code=400, detail="Not connected to Navidrome")

    svc = SyncService(client, user["id"])
    set_client_for_user(user["id"], client, svc)

    asyncio.create_task(_run_sync_in_background(user["id"], svc))

    return SyncStatus(
        is_syncing=True,
        last_sync=svc.last_sync,
        message="Sync started",
    )


# ── Last.fm config (protected, per-user) ─────────────────────────────────────


@router.get("/lastfm", response_model=LastfmConfigResponse)
async def get_lastfm_config_endpoint(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return whether the caller has Last.fm configured (never the API key)."""
    config = get_lastfm_config(db, user["id"])
    if config is None:
        return LastfmConfigResponse(configured=False)
    return LastfmConfigResponse(configured=True, username=config.username)


@router.put("/lastfm", response_model=LastfmConfigResponse)
async def set_lastfm_config(
    req: LastfmConfigRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the caller's Last.fm API key + username for the recency trigger."""
    config = get_lastfm_config(db, user["id"])
    if config:
        config.api_key = req.api_key
        config.username = req.username
    else:
        db.add(LastfmUserConfig(
            user_id=user["id"], api_key=req.api_key, username=req.username
        ))
    db.commit()
    return LastfmConfigResponse(configured=True, username=req.username)


@router.delete("/lastfm", response_model=LastfmConfigResponse)
async def clear_lastfm_config(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the caller's saved Last.fm config."""
    config = get_lastfm_config(db, user["id"])
    if config is not None:
        db.delete(config)
        db.commit()
    return LastfmConfigResponse(configured=False)


# ── ListenBrainz config (protected, per-user) ────────────────────────────────


@router.get("/listenbrainz", response_model=ListenbrainzConfigResponse)
async def get_listenbrainz_config_endpoint(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return whether the caller has ListenBrainz linked (never the token)."""
    config = get_listenbrainz_config(db, user["id"])
    if config is None:
        return ListenbrainzConfigResponse(configured=False)
    return ListenbrainzConfigResponse(configured=True, username=config.username)


@router.put("/listenbrainz", response_model=ListenbrainzConfigResponse)
async def set_listenbrainz_config(
    req: ListenbrainzConfigRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the caller's ListenBrainz username + user token for the recency trigger."""
    config = get_listenbrainz_config(db, user["id"])
    if config:
        config.username = req.username
        config.user_token = req.token
    else:
        db.add(ListenbrainzUserConfig(
            user_id=user["id"], username=req.username, user_token=req.token
        ))
    db.commit()
    return ListenbrainzConfigResponse(configured=True, username=req.username)


@router.delete("/listenbrainz", response_model=ListenbrainzConfigResponse)
async def clear_listenbrainz_config(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the caller's saved ListenBrainz config."""
    config = get_listenbrainz_config(db, user["id"])
    if config is not None:
        db.delete(config)
        db.commit()
    return ListenbrainzConfigResponse(configured=False)


@router.post("/listenbrainz/test", response_model=ListenbrainzTestResponse)
async def test_listenbrainz_config(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate the caller's saved ListenBrainz token and report the listen count.

    The count matters: with scrobbling just enabled the account is empty, and
    ListenBrainz-backed triggers fall back to Navidrome history until it fills.
    Surfacing the number is how the user knows data is actually flowing.
    """
    config = get_listenbrainz_config(db, user["id"])
    if config is None:
        raise HTTPException(status_code=400, detail="ListenBrainz is not configured")

    client = ListenBrainzClient(user_token=config.user_token, username=config.username)
    try:
        info = await client.validate_token()
        if not info.get("valid"):
            return ListenbrainzTestResponse(
                valid=False, username=config.username,
                message="Token rejected by ListenBrainz",
            )
        count = await client.get_listen_count()
        return ListenbrainzTestResponse(
            valid=True, username=config.username, listen_count=count,
            message=f"{count} listens",
        )
    except UserNotFoundError:
        return ListenbrainzTestResponse(
            valid=False, username=config.username,
            message="User not found on ListenBrainz",
        )
    except ListenBrainzError as e:
        return ListenbrainzTestResponse(
            valid=False, username=config.username, message=str(e),
        )
    finally:
        await client.close()


# ── Legacy /connect + /disconnect (backwards compat, protected) ──────────────

@router.post("/connect", response_model=ConnectionStatus)
async def legacy_connect(config: LoginRequest, db: Session = Depends(get_db), _user: dict = Depends(get_current_user)):
    """Legacy endpoint: same connect logic as /login but returns status."""
    try:
        _user_row, _client, version = await _connect(db, config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ConnectionStatus(
        connected=True,
        message=f"Connected to Navidrome v{version}",
        server_version=version,
    )


@router.post("/disconnect", response_model=ConnectionStatus)
async def legacy_disconnect(user: dict = Depends(get_current_user)):
    """Legacy endpoint: alias for logout — drops the caller's connection only."""
    await clear_clients_for_user(user["id"])
    return ConnectionStatus(connected=False, message="Disconnected")
