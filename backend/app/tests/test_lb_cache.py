"""Tests for the ListenBrainz TTL caches."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    LbRecommendationCache,
    LbSimilarCache,
    User,
)
from app.services.lb_cache import (
    RECOMMENDATION_TTL,
    SIMILAR_TTL,
    get_cached_recommendations,
    get_cached_similar,
    put_recommendations,
    put_similar,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


class TestRecommendationCache:
    def test_miss_when_empty(self, db):
        assert get_cached_recommendations(db, 1) is None

    def test_fresh_entry_hits(self, db):
        now = datetime.utcnow()
        put_recommendations(db, 1, [{"recording_mbid": "m1", "score": 1.0}], now=now)
        cached = get_cached_recommendations(db, 1, now=now + timedelta(hours=1))
        assert cached == [{"recording_mbid": "m1", "score": 1.0}]

    def test_stale_entry_misses(self, db):
        now = datetime.utcnow()
        put_recommendations(db, 1, [{"recording_mbid": "m1"}], now=now)
        assert get_cached_recommendations(
            db, 1, now=now + timedelta(hours=25)) is None

    def test_expiry_boundary(self, db):
        now = datetime.utcnow()
        put_recommendations(db, 1, [], now=now)
        assert get_cached_recommendations(db, 1, now=now + RECOMMENDATION_TTL) == []
        assert get_cached_recommendations(
            db, 1, now=now + RECOMMENDATION_TTL + timedelta(seconds=1)) is None

    def test_put_replaces_existing(self, db):
        put_recommendations(db, 1, [{"recording_mbid": "old"}])
        put_recommendations(db, 1, [{"recording_mbid": "new"}])
        assert db.query(LbRecommendationCache).count() == 1
        assert get_cached_recommendations(db, 1)[0]["recording_mbid"] == "new"

    def test_empty_list_is_a_hit_not_a_miss(self, db):
        """Caching 'no recommendations' avoids re-calling a 204 every run."""
        put_recommendations(db, 1, [])
        assert get_cached_recommendations(db, 1) == []

    def test_corrupt_json_degrades_to_empty(self, db):
        db.add(LbRecommendationCache(user_id=1, mbids="{not json"))
        db.commit()
        assert get_cached_recommendations(db, 1) == []

    def test_recommendations_do_not_leak_between_users(self, db):
        """CF recs are one listener's taste — never serve them to another."""
        put_recommendations(db, 1, [{"recording_mbid": "aaa"}])
        assert get_cached_recommendations(db, 2) is None
        assert get_cached_recommendations(db, 1)[0]["recording_mbid"] == "aaa"

    def test_each_user_keeps_their_own_row(self, db):
        put_recommendations(db, 1, [{"recording_mbid": "aaa"}])
        put_recommendations(db, 2, [{"recording_mbid": "bbb"}])
        assert db.query(LbRecommendationCache).count() == 2
        assert get_cached_recommendations(db, 1)[0]["recording_mbid"] == "aaa"
        assert get_cached_recommendations(db, 2)[0]["recording_mbid"] == "bbb"

    def test_cache_row_has_a_real_user_owner(self, db):
        """The PK is a FK to users, declared like TrackAnnotation's."""
        user = User(username="u1", navidrome_url="http://nav:4533",
                    navidrome_password="p", is_owner=True)
        db.add(user)
        db.commit()
        put_recommendations(db, user.id, [{"recording_mbid": "aaa"}])
        assert get_cached_recommendations(db, user.id)[0]["recording_mbid"] == "aaa"
        fk_targets = {
            fk_col.target_fullname
            for fk_col in LbRecommendationCache.__table__.c.user_id.foreign_keys
        }
        assert fk_targets == {"users.id"}


class TestSimilarCache:
    def test_miss_when_empty(self, db):
        assert get_cached_similar(db, "seed1") is None

    def test_roundtrip(self, db):
        put_similar(db, "seed1", [{"recording_mbid": "sim1", "score": 163}])
        assert get_cached_similar(db, "seed1")[0]["recording_mbid"] == "sim1"

    def test_keyed_per_recording(self, db):
        put_similar(db, "seed1", [{"recording_mbid": "a"}])
        put_similar(db, "seed2", [{"recording_mbid": "b"}])
        assert get_cached_similar(db, "seed1")[0]["recording_mbid"] == "a"
        assert get_cached_similar(db, "seed2")[0]["recording_mbid"] == "b"

    def test_stale_entry_misses(self, db):
        now = datetime.utcnow()
        put_similar(db, "seed1", [{"recording_mbid": "a"}], now=now)
        assert get_cached_similar(db, "seed1", now=now + timedelta(days=8)) is None
        assert get_cached_similar(db, "seed1", now=now + timedelta(days=6)) is not None

    def test_refresh_overwrites_same_row(self, db):
        put_similar(db, "seed1", [{"recording_mbid": "old"}])
        put_similar(db, "seed1", [{"recording_mbid": "new"}])
        assert db.query(LbSimilarCache).count() == 1
        assert get_cached_similar(db, "seed1")[0]["recording_mbid"] == "new"

    def test_empty_list_cached(self, db):
        """Labs returns [] for MBIDs with no neighbours — that is a fact, cache it."""
        put_similar(db, "seed1", [])
        assert get_cached_similar(db, "seed1") == []

    def test_blank_mbid_is_ignored(self, db):
        put_similar(db, "", [{"recording_mbid": "a"}])
        assert db.query(LbSimilarCache).count() == 0
        assert get_cached_similar(db, "") is None

    def test_similar_cache_is_still_global(self, db):
        """Neighbours belong to the RECORDING, so every user shares one row.

        This asserts the design decision: no user_id column, one row per MBID,
        visible to any caller.
        """
        put_similar(db, "mbid-1", [{"x": 1}])
        assert get_cached_similar(db, "mbid-1") == [{"x": 1}]
        assert db.query(LbSimilarCache).count() == 1
        assert "user_id" not in LbSimilarCache.__table__.columns
