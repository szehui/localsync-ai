"""Tests for ListenBrainz config and cache tables."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    LbRecommendationCache,
    LbSimilarCache,
    ListenbrainzConfig,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


class TestListenbrainzConfig:
    def test_singleton_roundtrip(self, db_session):
        db_session.add(ListenbrainzConfig(id=1, user_token="tok", username="sze"))
        db_session.commit()

        cfg = db_session.query(ListenbrainzConfig).filter(
            ListenbrainzConfig.id == 1).first()
        assert cfg.username == "sze"
        assert cfg.user_token == "tok"

    def test_defaults(self, db_session):
        db_session.add(ListenbrainzConfig(id=1, user_token="tok", username="sze"))
        db_session.commit()

        cfg = db_session.query(ListenbrainzConfig).first()
        assert cfg.enabled is True
        assert cfg.api_base == "https://api.listenbrainz.org"
        assert cfg.labs_api_base == "https://labs.api.listenbrainz.org"
        assert cfg.last_connect_check is None

    def test_can_be_cleared(self, db_session):
        db_session.add(ListenbrainzConfig(id=1, user_token="tok", username="sze"))
        db_session.commit()
        db_session.query(ListenbrainzConfig).filter(ListenbrainzConfig.id == 1).delete()
        db_session.commit()
        assert db_session.query(ListenbrainzConfig).count() == 0


class TestLbRecommendationCache:
    def test_roundtrip(self, db_session):
        payload = [{"recording_mbid": "abc", "score": 9.3}]
        db_session.add(LbRecommendationCache(id=1, mbids=json.dumps(payload)))
        db_session.commit()

        row = db_session.query(LbRecommendationCache).filter(
            LbRecommendationCache.id == 1).first()
        assert json.loads(row.mbids)[0]["recording_mbid"] == "abc"
        assert row.fetched_at is not None

    def test_fetched_at_is_set(self, db_session):
        db_session.add(LbRecommendationCache(id=1, mbids="[]"))
        db_session.commit()
        row = db_session.query(LbRecommendationCache).first()
        assert isinstance(row.fetched_at, datetime)
        assert datetime.utcnow() - row.fetched_at < timedelta(minutes=1)


class TestLbSimilarCache:
    def test_roundtrip(self, db_session):
        payload = [{"recording_mbid": "sim1", "score": 163}]
        db_session.add(LbSimilarCache(
            recording_mbid="seed1", payload=json.dumps(payload)))
        db_session.commit()

        row = db_session.query(LbSimilarCache).first()
        assert row.recording_mbid == "seed1"
        assert json.loads(row.payload)[0]["recording_mbid"] == "sim1"

    def test_keyed_per_recording(self, db_session):
        db_session.add(LbSimilarCache(recording_mbid="a", payload="[]"))
        db_session.add(LbSimilarCache(recording_mbid="b", payload="[]"))
        db_session.commit()

        assert db_session.query(LbSimilarCache).filter(
            LbSimilarCache.recording_mbid == "a").count() == 1
        assert db_session.query(LbSimilarCache).count() == 2
