"""Tests for the local MBID / text resolver."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Track
from app.services.resolver import (
    resolve_entries,
    resolve_mbids,
    resolve_text,
)
from app.services.sync import build_search_key


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        Track(id="s1", title="Some Resolve", artist_name="Röyksopp",
              recording_mbid="mbid-1", search_key=build_search_key("Röyksopp", "Some Resolve")),
        Track(id="s2", title="Hey Jude", artist_name="The Beatles",
              recording_mbid=None, search_key=build_search_key("The Beatles", "Hey Jude")),
        Track(id="s3", title="Untagged", artist_name="Nobody",
              recording_mbid=None, search_key=build_search_key("Nobody", "Untagged")),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.close()


class TestResolveMbids:
    def test_hit(self, db):
        assert resolve_mbids(db, ["mbid-1"]) == {"mbid-1": "s1"}

    def test_miss(self, db):
        assert resolve_mbids(db, ["unknown-mbid"]) == {}

    def test_mixed_and_deduped(self, db):
        assert resolve_mbids(db, ["mbid-1", "mbid-1", "nope"]) == {"mbid-1": "s1"}

    def test_empty_and_none_inputs_ignored(self, db):
        assert resolve_mbids(db, []) == {}
        assert resolve_mbids(db, [None, ""]) == {}


class TestResolveText:
    def test_hit_across_case_and_accents(self, db):
        assert resolve_text(db, [("royksopp", "some resolve")]) == {
            build_search_key("royksopp", "some resolve"): "s1"}

    def test_miss(self, db):
        assert resolve_text(db, [("Unknown", "Song")]) == {}

    def test_blank_key_ignored(self, db):
        assert resolve_text(db, [(None, None)]) == {}


class TestResolveEntries:
    def test_prefers_mbid(self, db):
        ids, unresolved = resolve_entries(db, [
            {"artist": "x", "title": "y", "recording_mbid": "mbid-1"},
        ])
        assert ids == ["s1"]
        assert unresolved == []

    def test_text_fallback_when_mbid_unknown(self, db):
        """LB knows an MBID but the library copy is an untagged rip."""
        ids, unresolved = resolve_entries(db, [
            {"artist": "The Beatles", "title": "Hey Jude",
             "recording_mbid": "lb-knows-this-mbid"},
        ])
        assert ids == ["s2"]
        assert unresolved == []

    def test_text_only_entry_resolves(self, db):
        ids, _ = resolve_entries(db, [{"artist": "Nobody", "title": "Untagged"}])
        assert ids == ["s3"]

    def test_unresolved_returned_for_search3_fallback(self, db):
        ids, unresolved = resolve_entries(db, [
            {"artist": "Ghost", "title": "Song", "recording_mbid": "nope"},
        ])
        assert ids == []
        assert len(unresolved) == 1
        assert unresolved[0]["artist"] == "Ghost"

    def test_preserves_input_order_and_dedupes(self, db):
        ids, _ = resolve_entries(db, [
            {"artist": "Nobody", "title": "Untagged"},
            {"artist": "Röyksopp", "title": "Some Resolve"},
            {"artist": "Nobody", "title": "Untagged"},  # duplicate song
        ])
        assert ids == ["s3", "s1"]

    def test_mixed_resolved_and_unresolved(self, db):
        ids, unresolved = resolve_entries(db, [
            {"artist": "Röyksopp", "title": "Some Resolve"},
            {"artist": "Ghost", "title": "Nothing"},
        ])
        assert ids == ["s1"]
        assert len(unresolved) == 1

    def test_empty_input(self, db):
        assert resolve_entries(db, []) == ([], [])


class TestAmbiguity:
    def test_duplicate_search_key_picks_lowest_id(self, db, caplog):
        """Two library copies of the same song must resolve deterministically."""
        db.add_all([
            Track(id="zzz", title="Dup", artist_name="Artist",
                  search_key=build_search_key("Artist", "Dup")),
            Track(id="aaa", title="Dup", artist_name="Artist",
                  search_key=build_search_key("Artist", "Dup")),
        ])
        db.commit()

        with caplog.at_level("INFO"):
            resolved = resolve_text(db, [("Artist", "Dup")])

        assert resolved[build_search_key("Artist", "Dup")] == "aaa"
        assert "duplicate search keys" in caplog.text
