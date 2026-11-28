"""One availability predicate, used by both the cards and the reconciler.

If these two ever disagree, the app contradicts itself: search says "Request"
for an album the request list calls `available`, or vice versa. The tests below
pin the predicate itself so both call sites inherit one behaviour.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Album, Base
from app.services.catalog import (
    OPEN_STATUSES,
    OwnershipIndex,
    library_has_release_group,
    ownership_index,
)
from app.services.requests import CLOSED_STATUSES, STATUS_RANK


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


MBID = "6eac2e57-ee50-36f8-b0c4-c4c847a2c098"


def add_album(db, *, mbid=None, artist="Queen", name="A Night at the Opera",
              song_count=12, album_id="nav-1"):
    db.add(Album(id=album_id, name=name, artist_name=artist,
                 song_count=song_count, release_group_mbid=mbid))
    db.commit()


class TestPredicate:
    def test_mbid_is_exact_and_wins(self, db):
        add_album(db, mbid=MBID, artist="Someone Else", name="Different")
        assert library_has_release_group(db, MBID, "x", "y") == "mbid"

    def test_mbid_must_match_exactly(self, db):
        add_album(db, mbid=MBID)
        assert library_has_release_group(db, MBID.upper(), "Queen",
                                         "A Night at the Opera") in ("name", None)
        other = "11111111-2222-3333-4444-555555555555"
        assert library_has_release_group(
            db, other, "Queen", "A Night at the Opera", song_count=99) is None

    def test_name_match_without_a_count_is_permissive(self, db):
        add_album(db, song_count=12)
        assert library_has_release_group(db, None, "Queen",
                                         "A Night at the Opera") == "name"

    def test_name_match_accepts_one_track_difference(self, db):
        """Editions differ by a bonus track; that is still the same album."""
        add_album(db, song_count=12)
        assert library_has_release_group(
            db, None, "Queen", "A Night at the Opera", song_count=13) == "name"
        assert library_has_release_group(
            db, None, "Queen", "A Night at the Opera", song_count=11) == "name"

    def test_name_match_rejects_a_different_length(self, db):
        add_album(db, song_count=12)
        assert library_has_release_group(
            db, None, "Queen", "A Night at the Opera", song_count=30) is None

    def test_same_title_different_artist_is_not_a_match(self, db):
        add_album(db, artist="Queen", name="Greatest Hits")
        assert library_has_release_group(
            db, None, "The Band", "Greatest Hits") is None

    def test_folding_ignores_case_punctuation_and_accents(self, db):
        add_album(db, artist="Röyksopp", name="The Inevitable End")
        assert library_has_release_group(
            db, None, "royksopp", "the inevitable end") == "name"

    def test_blank_title_never_matches(self, db):
        add_album(db)
        assert library_has_release_group(db, None, "Queen", "") is None

    def test_any_edition_count_satisfies_the_loose_check(self, db):
        """Two editions of one album: either count is acceptable."""
        add_album(db, song_count=12, album_id="nav-a")
        add_album(db, song_count=14, album_id="nav-b")
        index = ownership_index(db)
        assert index.lookup(None, "Queen", "A Night at the Opera", 14) == (
            True, "name")

    def test_empty_library_reports_nothing(self, db):
        assert library_has_release_group(db, MBID, "Queen", "Opera") is None


class TestIndexReuse:
    def test_a_passed_index_avoids_a_second_query(self, db):
        add_album(db, mbid=MBID)
        index = ownership_index(db)
        # A stale index must be believed: that is what makes reuse observable.
        db.query(Album).delete()
        db.commit()
        assert library_has_release_group(db, MBID, "Queen", "Opera",
                                         index=index) == "mbid"
        assert library_has_release_group(db, MBID, "Queen", "Opera") is None

    def test_index_is_built_from_rows(self):
        index = OwnershipIndex([(MBID, "Queen", "Opera", 12)])
        assert index.mbids == {MBID}
        assert index.lookup(MBID, "x", "y") == (True, "mbid")


class TestStatusSets:
    def test_open_and_closed_are_disjoint(self):
        assert not set(OPEN_STATUSES) & set(CLOSED_STATUSES)

    def test_available_is_not_open(self):
        """An album in the library should render as owned, not as a request."""
        assert "available" not in OPEN_STATUSES

    def test_every_open_status_has_a_rank(self):
        for status in OPEN_STATUSES:
            assert status in STATUS_RANK

    def test_failed_and_terminal_states_are_not_open(self):
        for status in ("failed", "denied", "cancelled"):
            assert status not in OPEN_STATUSES
