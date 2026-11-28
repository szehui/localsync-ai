"""Tests for localised-title aliases."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import AlbumAlias, Base
from app.services.album_aliases import (
    aliases_for,
    harvest,
    match,
    record_aliases,
)

MBID = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def detail(primary_title, *release_titles):
    return {
        "id": MBID,
        "title": primary_title,
        "releases": [{"title": t} for t in release_titles],
    }


class TestRecordAliases:
    def test_stores_localised_titles(self, db):
        # PSY — 싸다9 has an official release titled "PSY 9th".
        added = record_aliases(db, MBID, detail("싸다9", "PSY 9th", "싸다9"))
        assert added == ["PSY 9th"]
        assert aliases_for(db, MBID) == ["PSY 9th"]

    def test_primary_title_is_not_an_alias(self, db):
        assert record_aliases(db, MBID, detail("Hysteria", "Hysteria")) == []

    def test_is_idempotent(self, db):
        record_aliases(db, MBID, detail("싸다9", "PSY 9th"))
        assert record_aliases(db, MBID, detail("싸다9", "PSY 9th")) == []
        assert db.query(AlbumAlias).count() == 1

    def test_missing_releases_is_safe(self, db):
        assert record_aliases(db, MBID, {"id": MBID, "title": "X"}) == []

    def test_empty_mbid_is_safe(self, db):
        assert record_aliases(db, "", detail("싸다9", "PSY 9th")) == []

    def test_harvest_accumulates(self, db):
        second = "bbbbbbbb-0000-0000-0000-000000000002"
        n = harvest(db, [
            (MBID, detail("싸다9", "PSY 9th")),
            (second, {"id": second, "title": "화양연화", "releases": [{"title": "The Most Beautiful Moment in Life"}]}),
        ])
        assert n == 2


class TestMatch:
    def test_finds_release_group_by_localised_name(self, db):
        record_aliases(db, MBID, detail("싸다9", "PSY 9th"))
        assert match(db, "psy 9th") == {MBID: "PSY 9th"}

    def test_partial_query_matches(self, db):
        record_aliases(db, MBID, detail("싸다9", "PSY 9th"))
        assert match(db, "psy") == {MBID: "PSY 9th"}

    def test_short_query_is_ignored(self, db):
        # Below three folded characters a substring test matches everything.
        record_aliases(db, MBID, detail("싸다9", "PSY 9th"))
        assert match(db, "ps") == {}

    def test_empty_query_returns_nothing(self, db):
        record_aliases(db, MBID, detail("싸다9", "PSY 9th"))
        assert match(db, "") == {}

    def test_diacritics_fold(self, db):
        record_aliases(db, MBID, detail("Junior", "Röyksopp Junior"))
        assert match(db, "royksopp") == {MBID: "Röyksopp Junior"}

    def test_limit_is_respected(self, db):
        for i in range(5):
            mbid = f"cccccccc-0000-0000-0000-00000000000{i}"
            record_aliases(db, mbid, {"id": mbid, "title": f"X{i}",
                                      "releases": [{"title": f"Greatest Hits {i}"}]})
        assert len(match(db, "greatest hits", limit=2)) == 2
