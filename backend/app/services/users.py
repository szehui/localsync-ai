"""Per-user (per-tenant) state: Navidrome clients, sync services, credentials.

Everything in this module used to be a process-wide singleton: one Navidrome
client, one ``SyncService``, one set of credentials. With multiple tenants that
is no longer safe — two users can point at different Navidrome servers, so a
shared client would leak one user's library into another's requests.

Ownership of state is split in two on purpose:

* **The in-process registry** (``_clients`` / ``_sync_services``) holds only the
  clients that request handlers are actively using. It makes repeat requests
  cheap and keeps a single warmed client per user.
* **The database** (``users`` plus the per-user config tables) is the source of
  truth for credentials. APScheduler jobs never read the registry — they call
  :func:`build_client`, which builds a throwaway client straight from the
  stored row. That way a restart, or a user who happens to be logged out, does
  not break the nightly jobs.
"""
import logging
from datetime import datetime
from typing import Any, Optional

from app.models.database import (
    LastfmUserConfig,
    ListenbrainzUserConfig,
    User,
)
from app.services.navidrome import NavidromeClient

logger = logging.getLogger(__name__)

# user.id -> live client / sync service. Only request handlers populate these;
# see the module docstring for why scheduler jobs deliberately avoid them.
_clients: dict[int, NavidromeClient] = {}
_sync_services: dict[int, Any] = {}


def get_or_create_user(db, url: str, username: str, password: str) -> User:
    """Return the tenant for ``(url, username)``, creating it on first login.

    Lookup is on the (url, username) pair, never username alone: the same
    username on two different Navidrome servers is two distinct tenants.

    When the row already exists only the password is refreshed. A successful
    login is the only signal we get that a Navidrome password changed, and the
    scheduler replays the stored password for background jobs — if we did not
    refresh it those jobs would start failing with 401s. Nothing else is
    overwritten, so a display_name the user edited in the UI survives.
    """
    user = (
        db.query(User)
        .filter(User.navidrome_url == url, User.username == username)
        .first()
    )

    if user is None:
        user = User(
            username=username,
            display_name=username,
            navidrome_url=url,
            navidrome_password=password,
        )
        db.add(user)
        logger.info("Created user '%s' for %s", username, url)
    else:
        user.navidrome_password = password

    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


def get_user(db, user_id: int) -> Optional[User]:
    """Load a user row by id, or None when the id is unknown."""
    return db.get(User, user_id)


def set_client_for_user(
    user_id: int,
    client: NavidromeClient,
    sync_service: Any = None,
) -> None:
    """Publish a live client (and optionally its sync service) for a user."""
    _clients[user_id] = client
    if sync_service is not None:
        _sync_services[user_id] = sync_service


def peek_client(user_id: int) -> Optional[NavidromeClient]:
    """Return the cached client without connecting or validating it."""
    return _clients.get(user_id)


def peek_sync_service(user_id: int) -> Any:
    """Return the cached sync service without connecting or validating it."""
    return _sync_services.get(user_id)


async def clear_clients_for_user(user_id: int) -> None:
    """Drop one user's cached client and sync service, closing the client.

    Scoped strictly to ``user_id``: logging one user out must not tear down
    another user's sockets, so this never touches the rest of the registry.
    """
    _sync_services.pop(user_id, None)
    client = _clients.pop(user_id, None)
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        # A leaked httpx connection is noise; a failed logout is not. The
        # client is already out of the registry either way.
        logger.warning(
            "Failed to close Navidrome client for user %s", user_id, exc_info=True
        )


async def get_client(db, user_id: int) -> NavidromeClient:
    """Return a connected client for a request handler, reusing the cache.

    The cached client is pinged before it is handed out: httpx pools keep the
    object alive long after the server or network dropped the underlying
    sockets, so an unverified cached client can fail mid-request. A stale one
    is evicted and rebuilt from the stored credentials.
    """
    cached = _clients.get(user_id)
    if cached is not None:
        try:
            await cached.ping()
            return cached
        except Exception:
            logger.info(
                "Cached Navidrome client for user %s is stale; reconnecting", user_id
            )
            await clear_clients_for_user(user_id)

    user = get_user(db, user_id)
    if user is None:
        raise RuntimeError(f"Unknown user id {user_id}")

    client = NavidromeClient(
        url=user.navidrome_url,
        username=user.username,
        password=user.navidrome_password,
    )
    await client.ping()
    _clients[user_id] = client
    return client


def build_client(db, user_id: int) -> NavidromeClient:
    """Build a throwaway client from stored credentials for a scheduler job.

    Deliberately synchronous and registry-free: background jobs must not depend
    on whoever happens to be logged in, nor on the cached client surviving a
    restart. Callers own the returned client and must close it themselves.
    """
    user = get_user(db, user_id)
    if user is None:
        raise RuntimeError(f"Unknown user id {user_id}")

    return NavidromeClient(
        url=user.navidrome_url,
        username=user.username,
        password=user.navidrome_password,
    )


def get_listenbrainz_config(db, user_id: int) -> Optional[ListenbrainzUserConfig]:
    """Return the user's ListenBrainz row, or None if they never linked it."""
    return db.get(ListenbrainzUserConfig, user_id)


def get_lastfm_config(db, user_id: int) -> Optional[LastfmUserConfig]:
    """Return the user's Last.fm row, or None if they never linked it."""
    return db.get(LastfmUserConfig, user_id)
