"""Tests for the upstream catalog cache."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, CatalogCache
from app.services import catalog_cache


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_make_key_normalises_parts():
    assert catalog_cache.make_key("search", "Daft Punk", 0) == "search:daft punk:0"


def test_make_key_skips_none():
    assert catalog_cache.make_key("album", "abc", None) == "album:abc"


def test_miss_returns_none(db):
    assert catalog_cache.get(db, "search:nope:0", "search") is None


def test_put_then_get_round_trips(db):
    payload = {"release_groups": [{"release_group_mbid": "x", "title": "T"}], "count": 1}
    catalog_cache.put(db, "search:daft punk:0", "search", payload)
    assert catalog_cache.get(db, "search:daft punk:0", "search") == payload


def test_non_ascii_survives_round_trip(db):
    payload = {"title": "싸다9", "also_known_as": ["PSY 9th"]}
    catalog_cache.put(db, "album:psy", "album", payload)
    assert catalog_cache.get(db, "album:psy", "album") == payload


def test_stale_entry_is_deleted_and_returns_none(db):
    catalog_cache.put(db, "search:old:0", "search", {"x": 1})
    row = db.query(CatalogCache).filter_by(key="search:old:0").one()
    # search TTL is 10 minutes; push the row an hour into the past.
    row.fetched_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()

    assert catalog_cache.get(db, "search:old:0", "search") is None
    assert db.query(CatalogCache).filter_by(key="search:old:0").count() == 0


def test_long_lived_kind_tolerates_age(db):
    catalog_cache.put(db, "album:abc", "album", {"x": 1})
    row = db.query(CatalogCache).filter_by(key="album:abc").one()
    row.fetched_at = datetime.utcnow() - timedelta(days=2)  # album TTL is 7 days
    db.commit()

    assert catalog_cache.get(db, "album:abc", "album") == {"x": 1}


def test_put_overwrites_existing_row(db):
    catalog_cache.put(db, "album:abc", "album", {"v": 1})
    catalog_cache.put(db, "album:abc", "album", {"v": 2})
    assert db.query(CatalogCache).filter_by(key="album:abc").count() == 1
    assert catalog_cache.get(db, "album:abc", "album") == {"v": 2}


def test_corrupt_payload_is_discarded(db):
    db.add(CatalogCache(key="album:bad", kind="album", payload="{not json",
                        fetched_at=datetime.utcnow()))
    db.commit()
    assert catalog_cache.get(db, "album:bad", "album") is None
    assert db.query(CatalogCache).filter_by(key="album:bad").count() == 0


def test_negative_coverart_is_cacheable(db):
    # Cover Art Archive 404s are common and must be cached too, or every card
    # re-asks for artwork that does not exist.
    catalog_cache.put(db, "coverart:abc", "coverart", {"url": None})
    assert catalog_cache.get(db, "coverart:abc", "coverart") == {"url": None}


def test_clear_by_kind(db):
    catalog_cache.put(db, "search:a:0", "search", {})
    catalog_cache.put(db, "album:a", "album", {})
    assert catalog_cache.clear(db, "search") == 1
    assert db.query(CatalogCache).count() == 1


def test_clear_all(db):
    catalog_cache.put(db, "search:a:0", "search", {})
    catalog_cache.put(db, "album:a", "album", {})
    assert catalog_cache.clear(db) == 2
    assert db.query(CatalogCache).count() == 0
