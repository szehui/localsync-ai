# localsync-ai Multi-User Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make localsync-ai genuinely multi-tenant, so each household member's playlists
are generated from their own Navidrome annotations and their own ListenBrainz history,
without one person's login destroying another's data.

**Architecture:** Library *metadata* stays global (one `tracks`/`albums`/`artists` set
per Navidrome server). The *user-dependent* part of a track — play count, rating,
starred, last-played — moves out of the `tracks` row into a
`track_annotations(user_id, track_id)` table. Every credential singleton
(`ConnectionConfig`, `ListenbrainzConfig`, `LastfmConfig`) becomes a per-user row.
`SmartTrigger` and `GeneratedPlaylist` gain an `owner_id`. The scheduler fans out one
APScheduler job per (user, trigger) and each job builds its Navidrome client from that
user's stored credentials.

**Tech Stack:** FastAPI, SQLAlchemy 2.x ORM, SQLite (inline `ALTER TABLE` migrations in
`init_db()` — there is no Alembic), APScheduler, React frontend, pytest.

---

## Context an implementer must have

Read these before starting. Do not guess.

| File | Why |
| --- | --- |
| `backend/app/models/database.py` | All models; `init_db()` holds the inline migrations |
| `backend/app/routers/auth.py` | Login/JWT/global-client logic — the core of the problem |
| `backend/app/services/sync.py` | `_build_track`, `full_sync`, `_update_play_counts` |
| `backend/app/services/mixes.py` | Every library-mix selector reads `Track.play_count` |
| `backend/app/services/scheduler.py` | `_get_client_from_db`, `_publish_playlist`, per-trigger jobs |
| `backend/app/services/lb_cache.py` | CF recommendation cache (must become per-user) |
| `backend/app/services/discovery.py` | Seeds from `Track.play_count` (must become per-user) |

**Test command** (from `backend/`): `./venv/bin/python -m pytest app/tests -q`
Baseline: **387 passing**. Do not drop below that without explaining why.

**Style rules already in this repo — follow them:**
- Migrations are idempotent `ALTER TABLE`/`CREATE TABLE IF NOT EXISTS` blocks inside
  `init_db()`. No Alembic, no migration files.
- Tests use a real in-memory SQLite (`create_engine("sqlite:///:memory:")` +
  `Base.metadata.create_all`) when queries are the thing under test — see
  `app/tests/test_mixes.py`. MagicMock only for HTTP-shaped collaborators.
- `datetime.utcnow()` (naive UTC) everywhere. Do not introduce tz-aware datetimes.
- Comments explain *why*, not *what*. Match the existing voice.

---

## Decisions taken (and why)

1. **Annotations move to their own table.** `Track.play_count` is one column; Navidrome
   stores annotations per `(user_id, item_id)`. Trying to keep one column and
   "current user" flag would mean a sync per user visit and a cache that silently
   serves stale numbers. A join table is the honest model.
2. **Legacy `tracks.play_count` / `.rating` / `.starred` / `.last_played_at` columns stay
   in place, unwritten, for this release.** SQLite `DROP COLUMN` needs 3.35+ and the
   container's version is not pinned. They become dead weight; a cleanup task removes
   them once production has run a week. Tests that build `Track(play_count=…)` keep
   passing during the transition.
3. **Adopt, don't discard, the existing singleton data.** Whichever Navidrome user is in
   `ConnectionConfig` at migration time becomes `users.id = 1`, gets
   `is_owner = True`, and their `tracks.play_count` values are copied into
   `track_annotations` for them. Nothing is thrown away.
4. **`lb_similar_cache` stays global.** Correction to the earlier audit: Labs
   similar-recordings are a property of the *recording* (dataset neighbours), not of the
   listener. Sharing that cache across users is correct and saves calls. **Only
   `lb_recommendation_cache` is per-user**, because CF recommendations are computed from
   one listener's history.
5. **Playlist names are not prefixed.** Each user's playlists are created with that
   user's Subsonic credentials, so they land in that user's own Navidrome account.
   Two users can both have "Fresh Discoveries 2026-11-27" without colliding. Local
   `generated_playlists` rows are disambiguated by `owner_id` instead.
6. **Non-goal: app-level user management.** There is no signup, no password reset, no
   roles. A user exists once they have logged in with valid Navidrome credentials. Auth
   remains delegated.

## Known risk accepted, not fixed here

Navidrome passwords are stored in plaintext in SQLite (`ConnectionConfig` does this
today; the scheduler needs them to run while the user is offline). This plan keeps that
behaviour per-user. Encrypting at rest is a **separate** ticket — flagged in Task 18 so
it is not silently forgotten.

---

# Phase 1 — Data model and migration

### Task 1: Add `User` and `TrackAnnotation` models

**Objective:** Model a tenant and a per-(user, track) annotation.

**Files:**
- Modify: `backend/app/models/database.py` (imports; new classes after `Artist`)

**Step 1: Extend the imports**

```python
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, Boolean, Text,
    ForeignKey, UniqueConstraint,
)
```

**Step 2: Add the models**

Place immediately after the `Artist` class:

```python
class User(Base):
    """One household member with their own Navidrome login.

    Auth is still delegated to Navidrome — this row is created on first
    successful login and never carries a password of its own. The Navidrome
    password is kept so scheduled jobs can build a Subsonic client while the
    user is offline (see the plaintext-storage note in the plan).
    """

    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)        # Navidrome login
    display_name = Column(String, nullable=True)
    navidrome_url = Column(String, nullable=False)
    navidrome_password = Column(String, nullable=False)
    # The account that owned the pre-multi-user singleton data. Kept so the
    # migration can be audited and so a future "reset" knows whose row was
    # adopted rather than created.
    is_owner = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("navidrome_url", "username", name="uq_user_url_username"),
    )


class TrackAnnotation(Base):
    """Navidrome annotation for one track *as seen by one user*.

    Navidrome's own `annotation` table is keyed by (user_id, item_id), which is
    the model this mirrors: the same song is played 40 times by one person and
    never by another, and both facts are true simultaneously. Track metadata
    (title, artist, genre) lives on `Track` and is shared.
    """

    __tablename__ = "track_annotations"
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    track_id = Column(
        String, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    play_count = Column(Integer, default=0)
    rating = Column(Integer, default=0)
    starred = Column(Boolean, default=False)
    last_played_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

**Step 3: Verify it imports**

```bash
cd backend && ./venv/bin/python -c "from app.models.database import User, TrackAnnotation; print(User.__tablename__, TrackAnnotation.__tablename__)"
```
Expected: `users track_annotations`

**Step 4: Commit**

```bash
git add backend/app/models/database.py
git commit -m "feat(model): add User and TrackAnnotation for multi-tenancy"
```

---

### Task 2: Add per-user credential models

**Objective:** Per-user ListenBrainz and Last.fm config, replacing the singletons.

**Files:**
- Modify: `backend/app/models/database.py` (after `ListenbrainzConfig`)

**Step 1: Add the models**

Leave `ConnectionConfig`, `ListenbrainzConfig`, `LastfmConfig` in place — they are read
once by the migration and then never again.

```python
class ListenbrainzUserConfig(Base):
    """ListenBrainz credentials for one user.

    ListenBrainz auth is separate from Navidrome auth: a user who never linked
    their Navidrome scrobbler has no token, and their recency trigger falls back
    to Navidrome history alone. That is a valid state, so this row may be absent.
    """

    __tablename__ = "listenbrainz_user_config"
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    user_token = Column(String, nullable=False)
    username = Column(String, nullable=False)
    api_base = Column(String, default="https://api.listenbrainz.org")
    labs_api_base = Column(String, default="https://labs.api.listenbrainz.org")
    enabled = Column(Boolean, default=True)
    last_connect_check = Column(DateTime, nullable=True)


class LastfmUserConfig(Base):
    """Last.fm credentials for one user (fallback history source)."""

    __tablename__ = "lastfm_user_config"
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    api_key = Column(String, nullable=False)
    username = Column(String, nullable=False)
```

**Step 2: Verify**

```bash
cd backend && ./venv/bin/python -c "from app.models.database import ListenbrainzUserConfig, LastfmUserConfig; print('ok')"
```

**Step 3: Commit**

```bash
git add backend/app/models/database.py
git commit -m "feat(model): per-user ListenBrainz and Last.fm config"
```

---

### Task 3: Add `owner_id` to triggers and generated playlists

**Objective:** Give scheduled work and its output an owner.

**Files:**
- Modify: `backend/app/models/database.py` (`GeneratedPlaylist`, `SmartTrigger`)

**Step 1: Add the columns**

In `GeneratedPlaylist`, after `id`:

```python
    # NULL means "created before multi-user support" — the migration backfills
    # these to the owner user so no playlist becomes unreachable.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
```

In `SmartTrigger`, after `id`:

```python
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
```

**Step 2: Verify the models still build**

```bash
cd backend && ./venv/bin/python -c "
from app.models.database import GeneratedPlaylist, SmartTrigger
print([c.name for c in GeneratedPlaylist.__table__.columns])
print([c.name for c in SmartTrigger.__table__.columns])
"
```
Expected: both lists include `owner_id`.

**Step 3: Commit**

```bash
git add backend/app/models/database.py
git commit -m "feat(model): add owner_id to triggers and generated playlists"
```

---

### Task 4: Migration + backfill in `init_db()`

**Objective:** Create the new tables, adopt the legacy singleton data, and backfill.

**Files:**
- Modify: `backend/app/models/database.py` (`init_db()`, at the end of the function)

**Step 1: Write the migration block**

`Base.metadata.create_all(bind=engine)` already creates the new tables because they are
declared models. What remains is the *backfill*, which must run exactly once and be
safe on an already-migrated DB.

Append to `init_db()`:

```python
    # ── Multi-user migration (2026-11-27) ────────────────────────────────────
    # Adopt the pre-multi-user singleton so nothing is lost: whoever's
    # credentials were in `connection_config` becomes user #1 and inherits the
    # annotations currently sitting in the tracks table.
    from app.models.database import (  # local import: avoid module-level cycles
        User, TrackAnnotation, ConnectionConfig, ListenbrainzConfig, LastfmConfig,
        ListenbrainzUserConfig, LastfmUserConfig,
    )

    with SessionLocal() as db:
        already_migrated = db.query(User).first() is not None
        if not already_migrated:
            legacy_conn = db.query(ConnectionConfig).filter(ConnectionConfig.id == 1).first()
            if legacy_conn is None:
                logger.info("Multi-user migration: no legacy connection to adopt — skipping")
            else:
                owner = User(
                    username=legacy_conn.username,
                    display_name=legacy_conn.username,
                    navidrome_url=legacy_conn.url,
                    navidrome_password=legacy_conn.password,
                    is_owner=True,
                )
                db.add(owner)
                db.flush()  # assign owner.id

                # Carry over the ListenBrainz / Last.fm credentials, which were
                # also singletons. A missing row simply means "not configured".
                lb = db.query(ListenbrainzConfig).filter(ListenbrainzConfig.id == 1).first()
                if lb is not None:
                    db.add(ListenbrainzUserConfig(
                        user_id=owner.id, user_token=lb.user_token,
                        username=lb.username, api_base=lb.api_base,
                        labs_api_base=lb.labs_api_base, enabled=lb.enabled,
                    ))
                lfm = db.query(LastfmConfig).filter(LastfmConfig.id == 1).first()
                if lfm is not None:
                    db.add(LastfmUserConfig(
                        user_id=owner.id, api_key=lfm.api_key, username=lfm.username,
                    ))

                # Fold the legacy annotation columns into the new table. Only
                # rows that actually carry signal are copied: an all-zero row
                # is indistinguishable from "no row" and would bloat the table
                # by one insert per track.
                rows = (
                    db.query(Track)
                    .filter(
                        (Track.play_count > 0)
                        | (Track.starred.is_(True))
                        | (Track.last_played_at.isnot(None))
                        | (Track.rating > 0)
                    )
                    .all()
                )
                for t in rows:
                    db.add(TrackAnnotation(
                        user_id=owner.id, track_id=t.id,
                        play_count=t.play_count or 0, rating=t.rating or 0,
                        starred=bool(t.starred), last_played_at=t.last_played_at,
                    ))
                db.commit()
                logger.info(
                    "Multi-user migration: adopted '%s' as user 1 with %d annotations",
                    owner.username, len(rows),
                )
        else:
            logger.debug("Multi-user migration: users table already populated — skipping")

        # Backfill ownership of pre-existing triggers/playlists, every startup,
        # so an older row created by another code path still becomes reachable.
        owner = db.query(User).filter(User.is_owner.is_(True)).first()
        if owner is not None:
            changed = (
                db.query(SmartTrigger)
                .filter(SmartTrigger.owner_id.is_(None))
                .update({"owner_id": owner.id}, synchronize_session=False)
            )
            changed += (
                db.query(GeneratedPlaylist)
                .filter(GeneratedPlaylist.owner_id.is_(None))
                .update({"owner_id": owner.id}, synchronize_session=False)
            )
            if changed:
                db.commit()
                logger.info("Multi-user backfill: assigned %d legacy rows to owner", changed)
```

> **Careful:** `init_db()` already uses `engine.connect()` for its `ALTER TABLE` work and
> does not currently open a `SessionLocal()`. Confirm `SessionLocal` is in scope at the
> point this block runs (it is defined at module level) and that the block is placed
> *after* all `ALTER TABLE` statements, so the tables and columns exist.

**Step 2: Write the migration test**

Create `backend/app/tests/test_multi_user_migration.py`:

```python
"""The legacy singleton must be adopted, not discarded."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base, Track, User, TrackAnnotation, ConnectionConfig,
    ListenbrainzConfig, ListenbrainzUserConfig,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_legacy_connection_becomes_owner_user(db):
    db.add(ConnectionConfig(id=1, url="http://nav", username="sze", password="pw"))
    db.add(ListenbrainzConfig(id=1, user_token="tok", username="sze_lb"))
    db.add(Track(id="t1", title="Song", artist_name="A", play_count=17,
                 last_played_at=None, starred=True, rating=4))
    db.add(Track(id="t2", title="Silent", artist_name="A", play_count=0, rating=0))
    db.commit()

    # Call the migration helper directly (see Task 4, Step 3)
    from app.models.database import _migrate_multi_user
    _migrate_multi_user(db)

    owner = db.query(User).filter(User.is_owner.is_(True)).one()
    assert owner.username == "sze"
    assert owner.id == 1

    ann = db.query(TrackAnnotation).filter(TrackAnnotation.user_id == owner.id).all()
    assert {a.track_id for a in ann} == {"t1"}, "all-zero rows must not be copied"
    assert ann[0].play_count == 17
    assert ann[0].starred is True

    lb = db.query(ListenbrainzUserConfig).filter_by(user_id=owner.id).one()
    assert lb.username == "sze_lb"


def test_migration_is_idempotent(db):
    db.add(ConnectionConfig(id=1, url="http://nav", username="sze", password="pw"))
    db.commit()
    from app.models.database import _migrate_multi_user
    _migrate_multi_user(db)
    _migrate_multi_user(db)
    assert db.query(User).count() == 1
```

**Step 3: Extract the block into `_migrate_multi_user(db)`**

So it is testable without a live engine, factor the body into a module-level function in
`database.py`:

```python
def _migrate_multi_user(db) -> None:
    """Adopt legacy singleton rows into the users table. Idempotent."""
    ...  # the body written in Step 1, minus the `with SessionLocal()` wrapper
```

and have `init_db()` call it inside its own session. Run the test.

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_multi_user_migration.py -q
```
Expected: `2 passed`

**Step 4: Run the full suite**

```bash
cd backend && ./venv/bin/python -m pytest app/tests -q
```
Expected: 387 + 2 passing. Nothing should fail yet — no production code reads the new
tables.

**Step 5: Commit**

```bash
git add backend/app/models/database.py backend/app/tests/test_multi_user_migration.py
git commit -m "feat(db): migrate singleton config into per-user rows"
```

---

# Phase 2 — Identity

### Task 5: Carry the user id in the JWT

**Objective:** Make the token identify a tenant, not just a login.

**Files:**
- Modify: `backend/app/routers/auth.py` (`get_current_user`, `login`)
- Modify: `backend/app/services/auth.py` (nothing — `create_access_token` already takes a dict)

**Step 1: Write the failing test**

Add to `backend/app/tests/test_multi_user_auth.py`:

```python
"""Login must mint a token bound to a tenant id, not just a username."""
import jwt
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.main import app
from app.config import settings


def _decode(token):
    return jwt.decode(token, settings.jwt_secret_key,
                      algorithms=[settings.jwt_algorithm])


def test_token_carries_uid():
    claims = _decode(jwt.encode(
        {"sub": "sze", "url": "http://nav", "uid": 1, "exp": 9999999999},
        settings.jwt_secret_key, algorithm=settings.jwt_algorithm))
    assert claims["uid"] == 1
```

That test passes trivially today; the real assertion comes in Step 3's login test. Keep
it as the shape reference.

**Step 2: Update `login` to create or fetch the user and embed `uid`**

Replace the body of `login` in `backend/app/routers/auth.py`:

```python
@router.post("/login", response_model=TokenResponse)
async def login(creds: LoginRequest, db: Session = Depends(get_db)):
    """Validate Navidrome credentials, adopt/create the user, issue a JWT.

    The login IS the connect step. Multiple users coexist: each login updates
    that user's own row and their own cached client, leaving everyone else's
    connection untouched.
    """
    client = NavidromeClient(
        url=creds.url, username=creds.username, password=creds.password,
    )
    try:
        await client.ping()
    except Exception as e:
        await client.close()
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")

    user = get_or_create_user(
        db, url=creds.url, username=creds.username, password=creds.password,
    )
    set_client_for_user(user.id, client, SyncService(client))

    token = create_access_token(data={
        "sub": user.username,
        "url": user.navidrome_url,
        "uid": user.id,
    })
    return TokenResponse(access_token=token)
```

**Step 3: Update `get_current_user`**

```python
async def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """Verify the JWT and return the *tenant*, not just the login name.

    `uid` is what every caller uses to scope queries. Tokens minted before
    multi-user support have no `uid`; they are rejected rather than silently
    treated as user 1, because scoping to the wrong tenant is worse than a 401.
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
    return {
        "id": uid,
        "username": payload.get("sub", "unknown"),
        "navidrome_url": payload.get("url", ""),
    }
```

**Step 4: Run**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_auth.py app/tests/test_multi_user_auth.py -q
```
`test_auth.py` will fail on `get_or_create_user` (Task 6 not done). Expected for now —
proceed to Task 6 before evaluating.

---

### Task 6: `users.py` service — user CRUD and per-user client registry

**Objective:** Replace the module-global client with a dict keyed by user id, and give
the scheduler a way to build a client per user.

**Files:**
- Create: `backend/app/services/users.py`
- Test: `backend/app/tests/test_users_service.py`

**Step 1: Write the failing test**

```python
"""Per-user client registry and user upsert."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock

from app.models.database import Base, User
from app.services import users


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_get_or_create_user_is_stable(db):
    a = users.get_or_create_user(db, url="http://nav", username="sze", password="p1")
    b = users.get_or_create_user(db, url="http://nav", username="sze", password="p2")
    assert a.id == b.id
    assert db.query(User).count() == 1
    # The latest password wins — Navidrome passwords can be changed.
    assert db.query(User).one().navidrome_password == "p2"


def test_second_user_does_not_touch_the_first(db):
    a = users.get_or_create_user(db, url="http://nav", username="sze", password="p1")
    users.get_or_create_user(db, url="http://nav", username="rebecca", password="p2")
    fresh = db.query(User).filter_by(username="sze").one()
    assert fresh.id == a.id
    assert fresh.navidrome_password == "p1"


def test_client_registry_is_per_user():
    users.set_client_for_user(1, MagicMock(name="c1"), MagicMock(name="s1"))
    users.set_client_for_user(2, MagicMock(name="c2"), MagicMock(name="s2"))
    assert users.peek_client(1) is not users.peek_client(2)
    users.clear_clients_for_user(1)
    assert users.peek_client(1) is None
    assert users.peek_client(2) is not None, "clearing one user must not disconnect another"
```

**Step 2: Run and watch it fail**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_users_service.py -q
```
Expected: FAIL — `ModuleNotFoundError: app.services.users`

**Step 3: Implement `backend/app/services/users.py`**

```python
"""Tenant helpers: user upsert, per-user Navidrome clients, credential lookup.

Every piece of state that used to be a singleton lives here behind a user id.
The module owns one in-process registry of live clients; APScheduler jobs build
their own clients from the database instead, so a restart does not lose them.
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import (
    User, ListenbrainzUserConfig, LastfmUserConfig, SessionLocal,
)
from app.services.navidrome import NavidromeClient

logger = logging.getLogger(__name__)

_clients: dict[int, object] = {}
_sync_services: dict[int, object] = {}


# ── user rows ────────────────────────────────────────────────────────────────

def get_or_create_user(db: Session, url: str, username: str, password: str) -> User:
    """Find the user for this (url, username), creating it on first login.

    The password is refreshed on every login on purpose: it is the only signal
    we get that a Navidrome password changed, and the scheduler needs a working
    one. Nothing else on the row is overwritten, so display_name edits survive.
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


def get_user(db: Session, user_id: int) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


# ── client registry ──────────────────────────────────────────────────────────

def set_client_for_user(user_id: int, client, sync_service=None) -> None:
    _clients[user_id] = client
    if sync_service is not None:
        _sync_services[user_id] = sync_service


def peek_client(user_id: int):
    return _clients.get(user_id)


def peek_sync_service(user_id: int):
    return _sync_services.get(user_id)


async def clear_clients_for_user(user_id: int) -> None:
    """Disconnect one user. Other users keep their sockets."""
    client = _clients.pop(user_id, None)
    _sync_services.pop(user_id, None)
    if client is not None:
        try:
            await client.close()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to close client for user %s: %s", user_id, e)


async def get_client(db: Session, user_id: int) -> NavidromeClient:
    """Return a live client for `user_id`, reconnecting from saved creds if needed.

    Used by request handlers. Scheduler jobs call `build_client` instead so they
    never depend on whoever happens to be logged in.
    """
    client = _clients.get(user_id)
    if client is not None:
        try:
            await client.ping()
            return client
        except Exception:
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


def build_client(db: Session, user_id: int) -> NavidromeClient:
    """Construct a throwaway client from stored credentials (scheduler path)."""
    user = get_user(db, user_id)
    if user is None:
        raise RuntimeError(f"Unknown user id {user_id}")
    return NavidromeClient(
        url=user.navidrome_url,
        username=user.username,
        password=user.navidrome_password,
    )


# ── per-user upstream credentials ─────────────────────────────────────────────

def get_listenbrainz_config(db: Session, user_id: int) -> ListenbrainzUserConfig | None:
    return (
        db.query(ListenbrainzUserConfig)
        .filter(ListenbrainzUserConfig.user_id == user_id)
        .first()
    )


def get_lastfm_config(db: Session, user_id: int) -> LastfmUserConfig | None:
    return (
        db.query(LastfmUserConfig)
        .filter(LastfmUserConfig.user_id == user_id)
        .first()
    )
```

**Step 4: Re-run**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_users_service.py -q
```
Expected: `3 passed`

**Step 5: Commit**

```bash
git add backend/app/services/users.py backend/app/tests/test_users_service.py
git commit -m "feat(services): per-user client registry and credential lookup"
```

---

### Task 7: Rewire `auth.py` onto the registry

**Objective:** Delete the module globals; every endpoint acts on the caller's tenant.

**Files:**
- Modify: `backend/app/routers/auth.py` (imports, `_auto_connect`, `_ensure_connected`,
  `logout`, `status`, `sync-status`, `sync`, `me`, lastfm/listenbrainz config endpoints,
  `legacy_connect`, `legacy_disconnect`)

**Step 1: Add the user-scoped dependencies**

Replace the imports block at the top and the helpers section:

```python
from app.services.users import (
    get_or_create_user, get_user, get_client, build_client,
    set_client_for_user, peek_client, peek_sync_service, clear_clients_for_user,
    get_listenbrainz_config, get_lastfm_config,
)
from app.models.database import (
    get_db, SessionLocal, User,
    ListenbrainzUserConfig, LastfmUserConfig,
)
```

Delete `_current_client` and `_current_sync_service` and the two accessor functions.
Replace `get_navidrome_client` with a dependency that uses the caller (other routers
import it, so keep the name):

```python
async def get_navidrome_client(user: dict = Depends(get_current_user)):
    """FastAPI dependency: a live Navidrome client for the calling user."""
    db = SessionLocal()
    try:
        return await get_client(db, user["id"])
    except Exception as e:
        logger.warning("No usable Navidrome client for user %s: %s", user["id"], e)
        return None
    finally:
        db.close()
```

`playlists.py`, `library.py`, `triggers.py`, `discovery.py`, `youtube_import.py` all keep
their `client=Depends(get_navidrome_client)` calls unchanged. Nice: this is the one place
the refactor is nearly free.

**Step 2: `logout` clears only the caller**

```python
@router.post("/logout", response_model=ConnectionStatus)
async def logout(user: dict = Depends(get_current_user)):
    """Disconnect *this* user. The saved credentials stay so scheduled jobs run."""
    await clear_clients_for_user(user["id"])
    return ConnectionStatus(connected=False, message="Logged out")
```

Note the behaviour change: logout no longer deletes credentials. Leaving one user's
logout able to stop another's scheduled playlists was the worst bug in the old design;
if the user genuinely wants to remove an account that gets its own endpoint (Task 17).

**Step 3: `status` / `sync-status` / `sync`**

```python
@router.get("/status", response_model=ConnectionStatus)
async def connection_status(user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        client = await get_client(db, user["id"])
        result = await client.ping()
        version = result.get("version", "unknown")
        return ConnectionStatus(
            connected=True,
            message=f"Connected to Navidrome v{version}",
            server_version=version,
        )
    except Exception:
        return ConnectionStatus(connected=False, message="Not configured")
    finally:
        db.close()
```

```python
@router.post("/sync", response_model=SyncStatus)
async def trigger_sync(user: dict = Depends(get_current_user)):
    """Sync the caller's library metadata and their own annotations."""
    db = SessionLocal()
    try:
        client = await get_client(db, user["id"])
    finally:
        db.close()
    svc = SyncService(client, user_id=user["id"])
    set_client_for_user(user["id"], client, svc)
    if svc.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")
    asyncio.create_task(_run_sync_in_background(user["id"], svc))
    return SyncStatus(is_syncing=True, last_sync=svc.last_sync, message="Sync started")
```

```python
async def _run_sync_in_background(user_id: int, svc) -> None:
    db = SessionLocal()
    try:
        await svc.full_sync(db)   # Task 8 gives SyncService a user_id
    except Exception as e:
        logger.error("Background sync failed for user %s: %s", user_id, e)
    finally:
        db.close()
```

`sync_status` reads `peek_sync_service(user["id"])` and reports "Not connected" when it
is `None` — identical output shape, just per user.

**Step 4: lastfm / listenbrainz config endpoints**

Each becomes a per-user read/write. `get_lastfm_config` then:

```python
@router.get("/lastfm", response_model=LastfmConfigResponse)
async def get_lastfm_config_endpoint(user: dict = Depends(get_current_user),
                                     db: Session = Depends(get_db)):
    cfg = get_lastfm_config(db, user["id"])
    if cfg is None:
        return LastfmConfigResponse(configured=False)
    return LastfmConfigResponse(configured=True, username=cfg.username)
```

Apply the same shape to `PUT`/`DELETE` for both providers and to
`/listenbrainz/test`. **Watch the name collision:** the imported `get_lastfm_config`
from the service shadows nothing once the route function is renamed with an
`_endpoint` suffix — do rename it, the original bug-in-waiting is a silent recursion.

**Step 5: Delete `_auto_connect` / `_ensure_connected`**

Both are obsolete — `users.get_client` does reconnect lazily and per user. Remove them
along with `_current_client` references in `me`.

**Step 6: Run**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_auth.py -q
```
Expect failures in the tests that poke `auth_module._current_client` — those tests are
asserting the old singleton design. Update `test_auth.py`:

- `reset_globals` fixture → clear `app.services.users._clients` and `_sync_services`
  dicts, and the `users` table.
- `test_login_success` → assert `peek_client(user_id)` is not None instead of
  `auth_module._current_client`.
- `test_logout_clears_client` → assert `peek_client(1) is None`, and add
  `test_logout_leaves_other_users_connected`.

**Step 7: Commit**

```bash
git add backend/app/routers/auth.py backend/app/tests/test_auth.py
git commit -m "refactor(auth): scope login, logout and sync to the calling user"
```

---

# Phase 3 — Per-user sync

### Task 8: Split `_build_track` from annotation capture

**Objective:** Metadata writes stay global; play counts write to `track_annotations`.

**Files:**
- Modify: `backend/app/services/sync.py` (`_build_track`, new `_write_annotation`)
- Test: `backend/app/tests/test_sync_mbid.py` stays green (it tests metadata)

**Step 1: Make `_build_track` metadata-only**

Remove these four fields from the `Track(...)` construction in `_build_track`:

```python
        play_count=song_data.get("playCount", 0),
        rating=song_data.get("rating", 0),
        starred=_parse_starred(song_data.get("starred")),
        last_played_at=_parse_naive_utc(song_data.get("played")),
```

and add a companion that returns the annotation values:

```python
def _annotation_from_song(user_id: int, song_data: dict) -> TrackAnnotation:
    """The per-listener half of a Navidrome song object.

    These four fields were the only ones that differed between users; everything
    else about a song is library-wide metadata.
    """
    return TrackAnnotation(
        user_id=user_id,
        track_id=song_data["id"],
        play_count=song_data.get("playCount", 0) or 0,
        rating=song_data.get("rating", 0) or 0,
        starred=_parse_starred(song_data.get("starred")),
        last_played_at=_parse_naive_utc(song_data.get("played")),
    )
```

**Step 2: Give `SyncService` a `user_id`**

```python
class SyncService:
    def __init__(self, client, user_id: int):
        self.client = client
        self.user_id = user_id
        ...
```

Every construction site must pass it. Find them **including tests** — the tests will
otherwise break on the new required argument:

```bash
cd backend && grep -rn "SyncService(" app/ --include=*.py
```
Expected hits: `routers/auth.py` (4 sites) and the test modules `tests/test_sync.py`,
`tests/test_sync_prune.py` (5 sites). The scheduler does **not** construct a
`SyncService` — it builds a bare client and does its own fetching, so no change there.


**Step 3: Write the annotation in `full_sync` and `_update_play_counts`**

In `full_sync` step 3, alongside `db.merge(_build_track(song_data, album))`:

```python
                        db.merge(_build_track(song_data, album))
                        ann = _annotation_from_song(self.user_id, song_data)
                        db.merge(ann)
```

`_update_play_counts` becomes:

```python
    async def _update_play_counts(self, db: Session):
        """Refresh this user's annotations from Navidrome.

        Play counts are per Subsonic account, so this is deliberately scoped to
        `self.user_id`: another user's sync must not overwrite these numbers.
        """
        album_ids = [a.id for a in db.query(Album).all()]
        for album_id in album_ids:
            try:
                album_detail = await self.client.get_album(album_id)
                for song in album_detail.get("album", {}).get("song", []):
                    db.merge(_annotation_from_song(self.user_id, song))
            except Exception as e:
                logger.warning("Play-count update failed for album %s: %s", album_id, e)
                continue
        db.commit()
```

**Step 4: Test it**

Add `backend/app/tests/test_sync_multi_user.py`:

```python
"""One user's sync must not overwrite another user's play counts."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock

from app.models.database import Base, Track, TrackAnnotation, User
from app.services.sync import SyncService, _annotation_from_song


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_annotation_is_scoped_to_user(db):
    db.add(User(id=1, username="sze", navidrome_url="http://nav", navidrome_password="p"))
    db.add(User(id=2, username="rebecca", navidrome_url="http://nav", navidrome_password="p"))
    db.add(Track(id="t1", title="Song", artist_name="A"))
    db.commit()

    db.merge(_annotation_from_song(1, {"id": "t1", "playCount": 40}))
    db.merge(_annotation_from_song(2, {"id": "t1", "playCount": 2}))
    db.commit()

    sze = db.query(TrackAnnotation).filter_by(user_id=1, track_id="t1").one()
    reb = db.query(TrackAnnotation).filter_by(user_id=2, track_id="t1").one()
    assert (sze.play_count, reb.play_count) == (40, 2)
```

**Step 5: Commit**

```bash
git add backend/app/services/sync.py backend/app/tests/test_sync_multi_user.py
git commit -m "feat(sync): write annotations per user instead of onto tracks"
```

---

### Task 9: Per-user annotation reads for mixes

**Objective:** Every mix selector takes a `user_id`.

**Files:**
- Modify: `backend/app/services/mixes.py` (all `_SELECTORS` targets + `select_mix`)
- Test: `backend/app/tests/test_mixes.py` (add fixture `user_id=1`)

**Step 1: Add the join helper at the top of `mixes.py`**

```python
from sqlalchemy import and_, func, or_
from app.models.database import Track, TrackAnnotation


def annotated_tracks(db: Session, user_id: int):
    """`Track` rows left-joined to one user's annotations.

    LEFT JOIN, with coalesce on read: a track the user has never played has no
    annotation row and must still be selectable (that is exactly what the
    Unplayed mix needs). An inner join would silently shrink the library to
    only-what-you-have-played.
    """
    return (
        db.query(
            Track,
            func.coalesce(TrackAnnotation.play_count, 0).label("play_count"),
            func.coalesce(TrackAnnotation.rating, 0).label("rating"),
            func.coalesce(TrackAnnotation.starred, False).label("starred"),
            TrackAnnotation.last_played_at.label("last_played_at"),
        )
        .outerjoin(
            TrackAnnotation,
            and_(
                TrackAnnotation.track_id == Track.id,
                TrackAnnotation.user_id == user_id,
            ),
        )
    )
```

**Step 2: Rewrite one selector fully as the reference**

`select_on_repeat` currently filters on `Track.play_count`. Rewrite:

```python
def select_on_repeat(db: Session, user_id: int, limit: int = 50, rng=None,
                     min_plays: int = 5, within_days: int = 30, **_) -> list[dict]:
    """Tracks you have played at least `min_plays` times recently."""
    cutoff = datetime.utcnow() - timedelta(days=within_days)
    rows = (
        annotated_tracks(db, user_id)
        .filter(
            func.coalesce(TrackAnnotation.play_count, 0) >= min_plays,
            TrackAnnotation.last_played_at.isnot(None),
            TrackAnnotation.last_played_at >= cutoff,
        )
        .all()
    )
    ...
```

The rest of the body keeps operating on `(track, play_count, rating, starred,
last_played_at)` tuples instead of bare `Track` objects.

**Step 3: Apply the same signature to the other four**

`deep_cuts`, `rewind`, `rediscover`, `unplayed` each gain `user_id: int` as the second
positional parameter, and **every** `Track.play_count` / `.last_played_at` reference is
replaced by the joined column. `deep_cuts` groups by artist — group on
`Track.artist_name` as today, but order by the coalesced play count.

**Step 4: Thread it through `select_mix`**

```python
def select_mix(db: Session, user_id: int, kind: str, limit: int | None = None,
               rng=None, **overrides) -> list[dict]:
    ...
    return selector(db, user_id, limit=target, rng=rng, **spec)
```

**Step 5: Update the tests**

`test_mixes.py`'s `add_track` helper must write an annotation instead of a column:

```python
def add_track(db, user_id, track_id, artist, plays=0, genre="rock", last_played_days=None):
    db.merge(Track(id=track_id, title=f"Title {track_id}",
                   artist_name=artist, genre=genre))
    db.flush()
    db.merge(TrackAnnotation(
        user_id=user_id, track_id=track_id, play_count=plays,
        last_played_at=(None if last_played_days is None
                        else datetime.utcnow() - timedelta(days=last_played_days)),
    ))
    return track_id
```

and every `select_*(db, ...)` call gains `user_id=1`. Add one cross-tenant test:

```python
def test_mixes_are_isolated_per_user(db):
    db.merge(User(id=1, username="a", navidrome_url="http://nav", navidrome_password="p"))
    db.merge(User(id=2, username="b", navidrome_url="http://nav", navidrome_password="p"))
    add_track(db, 1, "t1", "Artist A", plays=10)
    add_track(db, 2, "t1", "Artist A", plays=0)
    db.commit()
    mine = select_on_repeat(db, user_id=1)
    theirs = select_on_repeat(db, user_id=2)
    assert [e["id"] for e in mine] == ["t1"]
    assert theirs == []
```

**Step 6: Run**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_mixes.py -q
```
Expected: all pass, including the isolation test.

**Step 7: Commit**

```bash
git add backend/app/services/mixes.py backend/app/tests/test_mixes.py
git commit -m "feat(mixes): select from per-user annotations"
```

---

### Task 10: Per-user CF recommendation cache

**Objective:** Stop serving one listener's collaborative-filtering recs to another.
(Labs similar-recordings stay global — they describe the recording.)

**Files:**
- Modify: `backend/app/models/database.py` (`LbRecommendationCache`)
- Modify: `backend/app/services/lb_cache.py`
- Test: `backend/app/tests/test_lb_cache.py`

**Step 1: Change the model key**

```python
class LbRecommendationCache(Base):
    """Cached user-level collaborative-filtering recommendations.

    Keyed by user: CF output is computed from one listener's history, so sharing
    a row between accounts would recommend one person's taste to another. The
    Labs *similar-recordings* cache is deliberately NOT per-user — neighbours of
    a recording are a property of the recording.
    """

    __tablename__ = "lb_recommendation_cache"
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mbids = Column(Text, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow)
```

The old table has `id` as PK and no `user_id`. `create_all` will not alter it. Add a
migration block in `init_db()`:

```python
    # lb_recommendation_cache: singleton -> per-user. The old row is dropped
    # rather than migrated: without a user id there is no correct owner, and a
    # 24h TTL means the only cost of dropping it is one extra API call.
    cols = [c["name"] for c in inspector.get_columns("lb_recommendation_cache")]
    if "user_id" not in cols:
        with engine.connect() as conn:
            conn.execute(sa.text("DROP TABLE lb_recommendation_cache"))
            conn.commit()
        Base.metadata.tables["lb_recommendation_cache"].create(bind=engine)
```

**Step 2: Thread `user_id` through the cache functions**

```python
def get_cached_recommendations(db: Session, user_id: int,
                               now: datetime | None = None) -> list[dict] | None:
    row = db.query(LbRecommendationCache).filter(
        LbRecommendationCache.user_id == user_id).first()
    ...

def put_recommendations(db: Session, user_id: int, mbids: list[dict],
                        now: datetime | None = None) -> None:
    ...
    row = db.query(LbRecommendationCache).filter(
        LbRecommendationCache.user_id == user_id).first()
    if row is None:
        db.add(LbRecommendationCache(user_id=user_id, mbids=payload, fetched_at=fetched_at))
    ...
```

`get_cached_similar` / `put_similar` are **unchanged**.

**Step 3: Update call sites**

```bash
cd backend && grep -rn "get_cached_recommendations\|put_recommendations" app/ --include=*.py
```
Each hit in `scheduler.py` / `discovery.py` gains the user id it already has in scope.

**Step 4: Update the test**

`test_lb_cache.py` gains `user_id` on every call and this case:

```python
def test_recommendations_do_not_leak_between_users(db):
    put_recommendations(db, user_id=1, mbids=[{"recording_mbid": "aaa"}])
    assert get_cached_recommendations(db, user_id=2) is None
    assert get_cached_recommendations(db, user_id=1)[0]["recording_mbid"] == "aaa"
```

**Step 5: Commit**

```bash
git add backend/app/models/database.py backend/app/services/lb_cache.py backend/app/tests/test_lb_cache.py
git commit -m "feat(cache): scope CF recommendations per user"
```

---

# Phase 4 — Per-user generation

### Task 11: Scope the library-mix runner and publishing to an owner

**Objective:** Mixes and playlists belong to a user end to end.

**Files:**
- Modify: `backend/app/services/scheduler.py` (`_run_library_mix`, `_publish_playlist`,
  `_get_cooldown_track_ids`, `_prune_old_dated_playlists`, `_recent_track_ids`)

**Step 1: Every `GeneratedPlaylist` query gains an owner filter**

There are four (`lines ~81, ~155, ~578, ~1155`). Pattern:

```python
    # Cooldown/aging history is per owner: my "recently used" tracks must not
    # suppress your mix.
    .filter(GeneratedPlaylist.owner_id == owner_id)
```

**Step 2: Wrap trigger functions so they know their owner**

The four entry points are `run_recency_trigger`, `run_heavy_rotation_trigger`,
`run_scheduled_trigger`, `run_library_mix_trigger` (`scheduler.py:777, 1019, 1247`, plus
the scheduled one), dispatched via the `TRIGGER_JOBS` map at `line ~1312`. Each takes a
`trigger_id` and loads the row itself. After the `trigger = db.query(SmartTrigger)...`
line, add:

```python
    owner_id = trigger.owner_id
    if owner_id is None:
        logger.error("Trigger %s has no owner — refusing to run", trigger.id)
        return
```

Then `_get_client_from_db()` (line ~614) call sites become:

```python
        client = build_client(db, owner_id)
```

and every `ListenbrainzConfig` / `LastfmConfig` singleton lookup becomes
`get_listenbrainz_config(db, owner_id)` / `get_lastfm_config(db, owner_id)`.
`select_mix(db, owner_id, kind, ...)` likewise. Delete `_get_client_from_db` once the
last call site is gone — leaving it would be a loaded gun that silently picks user 1.


**Step 3: `_publish_playlist` records the owner**

```python
async def _publish_playlist(db, client, name: str, track_ids, prune=False,
                            owner_id: int | None = None) -> bool:
    ...
    existing = (
        db.query(GeneratedPlaylist)
        .filter(GeneratedPlaylist.name == name,
                GeneratedPlaylist.owner_id == owner_id)
        .first()
    )
    ...
        db.add(GeneratedPlaylist(
            name=name,
            owner_id=owner_id,
            navidrome_playlist_id=result.get("playlist", {}).get("id", ""),
            track_count=len(track_ids),
            track_ids=json.dumps(track_ids),
        ))
```

**Step 4: Verify with the existing trigger tests**

```bash
cd backend && ./venv/bin/python -m pytest app/tests/test_triggers.py app/tests/test_heavy_rotation_lb.py -q
```
Fix the fixtures to create a `User` and set `owner_id` on triggers — the tests will
otherwise exercise the "no owner, refuse" branch.

**Step 5: Commit**

```bash
git add backend/app/services/scheduler.py backend/app/tests/test_triggers.py backend/app/tests/test_heavy_rotation_lb.py
git commit -m "feat(scheduler): generate and publish per owner"
```

---

### Task 12: Fan the scheduler out per user

**Objective:** One job per (user, trigger) instead of one per trigger.

**Files:**
- Modify: `backend/app/services/scheduler.py` (`_add_trigger_job`, `load_triggers`,
  `add_trigger_job`, job-id scheme)

**Step 1: Make job ids include the owner**

```python
def _job_id(trigger_id: int) -> str:
    """Job ids are per trigger row, and every trigger row has exactly one owner,
    so the owner does not need to appear in the id — the row carries it."""
    return f"trigger-{trigger_id}"
```

Keep the id scheme. The fan-out is not "same trigger, many users" — it is "each user
gets their own trigger rows". This is the smaller change and it matches how the UI
already creates triggers: when a user creates a trigger, it is theirs.

**Step 2: Filter trigger loading by owner at creation time, not at run time**

In `routers/triggers.py`, every list/create/update/delete handler takes
`user: dict = Depends(get_current_user)` and filters `SmartTrigger.owner_id == user["id"]`.
Creating a trigger sets `owner_id=user["id"]`. This is a required change: without it,
Rebecca sees and can edit Sze's triggers.

**Step 3: Test**

```python
def test_triggers_are_not_visible_across_users(client, ...):
    # Two users, each creates one trigger; GET /api/triggers returns only their own.
    ...
```

**Step 4: Commit**

```bash
git add backend/app/services/scheduler.py backend/app/routers/triggers.py backend/app/tests/
git commit -m "feat(scheduler): triggers are owned and isolated per user"
```

---

### Task 13: Per-user discovery seeds

**Objective:** The Discover feed reflects *your* listening, not the household's.

**Files:**
- Modify: `backend/app/services/discovery.py` (`select_seeds`, line ~109)
- Modify: `backend/app/routers/discovery.py`

**Step 1: Rewrite the seed query**

The existing function is `select_seeds(db, limit=DEFAULT_SEED_COUNT)`. Give it a
`user_id` and join the annotations table:

```python
def select_seeds(db: Session, user_id: int,
                 limit: int = DEFAULT_SEED_COUNT) -> list[Track]:
    """Top-played tracks carrying an MBID, at most one per artist, for THIS user.

    The join matters: a track the household played 300 times but this user never
    touched is not a seed for their discovery feed.
    """
    rows = (
        db.query(Track)
        .join(TrackAnnotation, and_(
            TrackAnnotation.track_id == Track.id,
            TrackAnnotation.user_id == user_id,
        ))
        .filter(TrackAnnotation.play_count > 0, Track.recording_mbid.isnot(None))
        .order_by(TrackAnnotation.play_count.desc(), Track.id)
        .all()
    )
    seen, out = set(), []
    for t in rows:
        if t.artist_name in seen:
            continue
        seen.add(t.artist_name)
        out.append(t)
        if len(out) >= limit:
            break
    return out
```

`refresh_feed(db, client, seed_count=...)` at line ~410 gains `user_id` and passes it
through, as do `upsert_offers` / `offered_feed` / `get_state`.

**Step 2: `DiscoveryOffer` needs an owner too**

`discovery_offers.recording_mbid` is currently `unique=True` (line 186), which means user
B can never be offered something user A was offered. Add a `user_id` column, drop the
single-column unique, and add `UniqueConstraint("user_id", "recording_mbid")`.
Migration in `init_db()`: add the column, backfill to owner, drop and recreate the unique
index. SQLite cannot drop a constraint in place — recreate the table (create
`discovery_offers_new`, `INSERT … SELECT`, rename), or simply drop the table and accept
losing the dedupe memory, which costs one round of re-offers.


**Step 3: Commit**

```bash
git add backend/app/services/discovery.py backend/app/routers/discovery.py backend/app/models/database.py
git commit -m "feat(discovery): per-user seeds and offers"
```

---

# Phase 5 — API surface, UI, and verification

### Task 14: User endpoints

**Objective:** Let the UI show who is logged in and let an owner remove an account.

**Files:**
- Create/modify: `backend/app/routers/auth.py`, `backend/app/models/schemas.py`

**Step 1: Add `GET /api/auth/users`**

Returns the household roster (id, username, display_name, whether LB is configured) —
**no passwords**.

**Step 2: Add `DELETE /api/auth/users/{user_id}`**

Owner-only (`user` for which `User.is_owner` is true). Deletes the user row; the
`ON DELETE CASCADE` foreign keys remove their annotations, LB/Lastfm config, and CF cache.
Their `generated_playlists` rows: keep, with `owner_id` nulled, so history survives.

**Step 3: Commit**

```bash
git add backend/app/routers/auth.py backend/app/models/schemas.py
git commit -m "feat(api): list and remove household users"
```

---

### Task 15: Frontend — show the current user

**Objective:** Make it obvious whose playlists you are looking at.

**Files:**
- Modify: `frontend/src/**` (wherever `/api/auth/me` is consumed)

**Step 1:** Display `username` in the header from the existing `/api/auth/me` response.

**Step 2:** On 401 with "Token predates multi-user support", clear the stored token and
show the login screen rather than a bare error.

**Step 3:** Verify by logging in as two different Navidrome users in two browser
profiles; each header shows their own name.

**Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): show the signed-in user"
```

---

### Task 16: Full-suite validation and live smoke test

**Objective:** Prove it works against the real Navidrome, not just the test DB.

**Step 1: Full test suite**

```bash
cd backend && ./venv/bin/python -m pytest app/tests -q
```
Expected: ≥389 passing (387 baseline + the new multi-user tests), 0 failures.

**Step 2: Back up the production DB before deploying**

```bash
ssh hui-server 'docker exec localsync-ai-localsync-1 cp /app/backend/data/localsync.db /app/backend/data/localsync.db.pre-multiuser'
```
(or `docker cp` it out to the host — either way, take the copy *before* the new image
starts, because `init_db()` migrates on boot.)

**Step 3: Deploy**

```bash
ssh hui-server 'cd ~/localsync-ai && docker compose up -d --build && docker logs -f localsync-ai-localsync-1'
```
Watch for `Multi-user migration: adopted '<username>' as user 1 with N annotations`.

**Step 4: Verify the adoption**

```bash
ssh hui-server 'docker exec localsync-ai-localsync-1 python -c "
import sqlite3
c = sqlite3.connect(\"/app/backend/data/localsync.db\")
print(\"users:\", c.execute(\"select id, username, is_owner from users\").fetchall())
print(\"annotations:\", c.execute(\"select count(*), max(play_count) from track_annotations\").fetchone())
print(\"top:\", c.execute(\"select track_id, play_count from track_annotations order by play_count desc limit 5\").fetchall())
"'
```
Expected: one user, annotation count roughly equal to the number of played tracks, and
top play counts matching what the UI showed before the migration.

**Step 5: Log in as the second user and confirm isolation**

1. Log in as user B in a private window → their `/api/auth/me` shows B.
2. `GET /api/auth/status` for B is connected; user A's session is still connected
   (the old code would have killed it).
3. Run B's sync, then re-check A's top annotations — **they must be unchanged.**

**Step 6: Trigger one of each and confirm the playlist lands in the right account**

- A's `Fresh Discoveries` appears in A's Navidrome playlists.
- B's library mix appears in B's, built from B's (thin) history.
- A "More Like This" from B's heavy rotation must not appear in A's account.

**Step 7: Commit any fixes, push**

```bash
git push origin main
```

---

### Task 17: Documentation

**Objective:** Leave a trail. This project already keeps `DEVLOG.md` and a wiki page.

**Files:**
- Modify: `DEVLOG.md`
- Modify: `~/wiki/domains/infrastructure/localsync-ai.md`

**Step 1: Replace the "Multi-user: not supported" section**

It becomes "Multi-user (2026-11-27)" describing the annotations table, the per-user
credential tables, the CF cache scoping, and the corrected note that Labs similar-cache
is intentionally global.

**Step 2: Record the operational facts**

- Adding a user = they log in once with their Navidrome credentials. Nothing to
  provision server-side.
- ListenBrainz is per-user and optional; without it that user's recency trigger uses
  Navidrome history only.
- Logout no longer deletes credentials — scheduled jobs keep running.
- **Open item: Navidrome passwords are stored in plaintext.** Track it as a follow-up
  (Options: OS keyring, a KMS-style env secret, or encrypt with `cryptography` using a key
  from the environment). Do not let this scroll away.

**Step 3: Commit**

```bash
git add DEVLOG.md
git commit -m "docs: multi-user architecture and operations"
```

---

### Task 18: Delete the dead annotation columns

**Objective:** Finish the job once production has run a week.

**Precondition:** Production has been on multi-user for ≥7 days and
`track_annotations` has sane counts for both users. **Do not do this early** — the old
columns are the rollback path.

**Step 1: Confirm SQLite version supports DROP COLUMN**

```bash
ssh hui-server 'docker exec localsync-ai-localsync-1 python -c "import sqlite3; print(sqlite3.sqlite_version)"'
```
Needs ≥3.35.0. If lower, leave the columns and close this task as "not worth it".

**Step 2:** `ALTER TABLE tracks DROP COLUMN play_count` (and `rating`, `starred`,
`last_played_at`), guarded by an `inspector` check in `init_db()`.

**Step 3:** Remove the legacy models `ConnectionConfig`, `ListenbrainzConfig`,
`LastfmConfig` and their migration reads (keep `_migrate_multi_user` able to no-op when
the tables are gone — wrap the queries in `inspector.has_table`).

**Step 4:** Full suite + deploy + commit.

---

## Rollback

Before Task 16's deploy the DB copy exists at
`/app/backend/data/localsync.db.pre-multiuser`. To roll back:

```bash
ssh hui-server 'cd ~/localsync-ai && git checkout <previous-sha> && docker compose up -d --build'
ssh hui-server 'docker exec localsync-ai-localsync-1 cp /app/backend/data/localsync.db.pre-multiuser /app/backend/data/localsync.db'
ssh hui-server 'cd ~/localsync-ai && docker compose restart'
```

The old code reads the singleton tables, which the migration did not delete — that is
why they are kept until Task 18.

## Definition of done

- [ ] Two Navidrome users can be logged in simultaneously; neither disconnects the other.
- [ ] Each user's `/api/auth/sync` writes annotations only for them.
- [ ] Library mixes for user B are computed from B's annotations.
- [ ] CF recommendations are per user; Labs similarity cache stays shared.
- [ ] Triggers, generated playlists, and discovery offers are owned and invisible across users.
- [ ] Logging out does not stop scheduled generation.
- [ ] ≥389 tests pass; no test asserts the old singleton behaviour.
- [ ] Wiki + DEVLOG updated, including the plaintext-password open item.
