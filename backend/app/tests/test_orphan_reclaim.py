"""Deleted-user history must NOT be reclaimed on the next startup.

`DELETE /users/{id}` nulls that user's GeneratedPlaylist.owner_id so the history
survives but stops pointing at a removed account. The multi-user migration used
to backfill every NULL owner_id on EVERY startup, which quietly handed those
rows back to the owner — resurrecting history the operator had deleted, and
defeating the "trigger has no owner — refusing to run" guard.

The backfill must therefore run only on the startup that performs the adoption.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    ConnectionConfig,
    GeneratedPlaylist,
    SmartTrigger,
    User,
    _migrate_multi_user,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_legacy(db, username="sze"):
    db.add(ConnectionConfig(id=1, url="https://nd.example", username=username,
                            password="pw"))
    db.commit()


def test_orphaned_history_is_not_reclaimed_after_adoption(db):
    """Adopt once, delete a user (nulling their history), start up again."""
    _seed_legacy(db)
    _migrate_multi_user(db)  # the adopting startup

    owner = db.query(User).filter_by(is_owner=True).one()

    # A second household member, with history of their own.
    member = User(username="rebecca", display_name="Rebecca",
                  navidrome_url="https://nd.example", navidrome_password="pw",
                  is_owner=False)
    db.add(member)
    db.flush()

    db.add(GeneratedPlaylist(name="Fresh Discoveries 2026-11-27",
                             owner_id=member.id))
    db.add(SmartTrigger(name="Rebecca Daily", trigger_type="recency",
                        owner_id=member.id))
    db.commit()

    # Simulate DELETE /users/{id}: history survives, ownership is released.
    member_id = member.id
    db.query(GeneratedPlaylist).filter(
        GeneratedPlaylist.owner_id == member_id
    ).update({"owner_id": None}, synchronize_session=False)
    db.query(SmartTrigger).filter(
        SmartTrigger.owner_id == member_id
    ).update({"owner_id": None}, synchronize_session=False)
    db.query(User).filter(User.id == member_id).delete()
    db.commit()

    # Restart: init_db() calls this again. Users table is NOT empty.
    _migrate_multi_user(db)

    playlist = db.query(GeneratedPlaylist).filter(
        GeneratedPlaylist.name == "Fresh Discoveries 2026-11-27"
    ).one()
    trigger = db.query(SmartTrigger).filter(
        SmartTrigger.name == "Rebecca Daily"
    ).one()

    assert playlist.owner_id is None, (
        "deleted user's playlist history was re-attributed to the owner"
    )
    assert trigger.owner_id is None, (
        "deleted user's trigger was re-attributed to the owner, so it would "
        "start generating again"
    )
    assert playlist.owner_id != owner.id
    assert trigger.owner_id != owner.id


def test_adoption_still_backfills_pre_existing_rows(db):
    """The legitimate case must keep working: rows that predate owner_id."""
    _seed_legacy(db)
    # Legacy rows exist BEFORE the adopting startup, with no owner.
    db.add(GeneratedPlaylist(name="Legacy Mix", owner_id=None))
    db.add(SmartTrigger(name="Legacy Daily", trigger_type="recency",
                        owner_id=None))
    db.commit()

    _migrate_multi_user(db)

    owner = db.query(User).filter_by(is_owner=True).one()
    assert db.query(GeneratedPlaylist).filter(
        GeneratedPlaylist.name == "Legacy Mix"
    ).one().owner_id == owner.id
    assert db.query(SmartTrigger).filter(
        SmartTrigger.name == "Legacy Daily"
    ).one().owner_id == owner.id
