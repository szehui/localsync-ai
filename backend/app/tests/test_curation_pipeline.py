"""Tests for scheduler-side curation: aging, capping, the publish gate.

`_curate` and `_aging_penalty` are the bridge between a raw blended track list
and a playlist worth listening to, and both read real DB state (the local track
index and previously generated playlists), so these run against in-memory
SQLite rather than mocks.
"""
import json
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, GeneratedPlaylist, Track
from app.services import scheduler
from app.services.curation import MIN_PLAYLIST_TRACKS
from app.services.scheduler import _aging_penalty, _curate, _publish_gate_ok


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def add_track(db, track_id, artist="Artist", genre="rock"):
    db.add(Track(id=track_id, title=track_id, artist_name=artist, genre=genre,
                 play_count=1))
    return track_id


class TestAgingPenalty:
    def test_recently_offered_track_is_penalised(self, db):
        add_track(db, "t1")
        db.add(GeneratedPlaylist(name="Mix", track_ids=json.dumps(["t1"]),
                                 updated_at=datetime.utcnow() - timedelta(days=1)))
        db.commit()
        penalties = _aging_penalty(db, ["t1"], datetime.utcnow())
        assert penalties["t1"] > 0

    def test_more_recent_offering_costs_more(self, db):
        """Yesterday's playlist is suppressed harder than one from 10 days ago."""
        add_track(db, "t1")
        add_track(db, "t2")
        db.add(GeneratedPlaylist(name="Old", track_ids=json.dumps(["t1"]),
                                 updated_at=datetime.utcnow() - timedelta(days=10)))
        db.add(GeneratedPlaylist(name="New", track_ids=json.dumps(["t2"]),
                                 updated_at=datetime.utcnow() - timedelta(days=1)))
        db.commit()
        penalties = _aging_penalty(db, ["t1", "t2"], datetime.utcnow())
        assert penalties["t2"] > penalties["t1"]

    def test_most_recent_offering_wins(self, db):
        """Two runs both contained the track: the newer one decides the penalty."""
        add_track(db, "t1")
        for days in (20, 2):
            db.add(GeneratedPlaylist(name=f"Mix {days}", track_ids=json.dumps(["t1"]),
                                     updated_at=datetime.utcnow() - timedelta(days=days)))
        db.commit()
        penalty = _aging_penalty(db, ["t1"], datetime.utcnow())["t1"]
        from app.services.curation import aging_penalty
        assert penalty == pytest.approx(aging_penalty(2), abs=0.05)

    def test_never_offered_track_has_no_penalty(self, db):
        assert "t1" not in _aging_penalty(db, ["t1"], datetime.utcnow())

    def test_malformed_json_is_skipped_not_fatal(self, db):
        db.add(GeneratedPlaylist(name="Broken", track_ids="{not json",
                                 updated_at=datetime.utcnow()))
        db.commit()
        assert _aging_penalty(db, ["t1"], datetime.utcnow()) == {}

    def test_none_track_ids_is_skipped(self, db):
        db.add(GeneratedPlaylist(name="Empty", track_ids=None, updated_at=datetime.utcnow()))
        db.commit()
        assert _aging_penalty(db, ["t1"], datetime.utcnow()) == {}

    def test_playlists_outside_the_lookback_are_ignored(self, db):
        add_track(db, "t1")
        db.add(GeneratedPlaylist(name="Ancient", track_ids=json.dumps(["t1"]),
                                 updated_at=datetime.utcnow() - timedelta(days=90)))
        db.commit()
        assert _aging_penalty(db, ["t1"], datetime.utcnow(), lookback_days=30) == {}


class TestCurate:
    def test_preserves_target_size_when_pool_is_ample(self, db):
        ids = [add_track(db, f"t{i}", f"Artist {i}") for i in range(60)]
        out = _curate(db, ids, target=50, rng=random.Random(0))
        assert len(out) == 50

    def test_caps_a_flooding_artist(self, db):
        """One artist owning most of the pool must not own most of the playlist."""
        ids = [add_track(db, f"a{i}", "Flooder") for i in range(20)]
        ids += [add_track(db, f"b{i}", f"Other {i}") for i in range(40)]
        out = _curate(db, ids, target=30, rng=random.Random(0))
        assert sum(1 for sid in out if sid.startswith("a")) <= 5

    def test_cap_does_not_starve_the_playlist(self, db):
        """Three artists, target 20: the cap is relaxed rather than shipping 15."""
        ids = [add_track(db, f"t{i}", f"Artist {i % 3}") for i in range(20)]
        out = _curate(db, ids, target=20, rng=random.Random(0))
        assert len(out) == 20

    def test_no_metadata_returns_blend_order_unchanged(self, db):
        """Fresh install: nothing in the local index, so don't reorder anything."""
        ids = ["x", "y", "z"]
        assert _curate(db, ids, target=50, rng=random.Random(0)) == ids

    def test_empty_input(self, db):
        assert _curate(db, [], target=50, rng=random.Random(0)) == []

    def test_deduplicates(self, db):
        add_track(db, "t1", "A")
        assert _curate(db, ["t1", "t1"], target=50, rng=random.Random(0)) == ["t1"]

    def test_recently_offered_tracks_sink(self, db):
        """Aging: yesterday's playlist must not simply be served again.

        Measured as displacement against the same pool with no prior run, since
        the seasoned tracks start at the top of the blend by construction — the
        question is whether they are pushed *down*, not whether they end up
        below everything.
        """
        ids = [add_track(db, f"t{i}", f"Artist {i}") for i in range(20)]
        baseline = _curate(db, ids, target=20, rng=random.Random(0))

        db.add(GeneratedPlaylist(
            name="Yesterday",
            track_ids=json.dumps(ids[:10]),
            updated_at=datetime.utcnow() - timedelta(hours=6),
        ))
        db.commit()
        aged = _curate(db, ids, target=20, rng=random.Random(0))

        stale = set(ids[:10])
        assert aged != baseline
        displacement = [
            aged.index(sid) - baseline.index(sid) for sid in stale
        ]
        assert sum(displacement) / len(displacement) > 0, aged

    def test_the_leader_of_yesterdays_playlist_is_displaced(self, db):
        """The concrete promise: the track that opened last run does not open this one.

        Staleness costs a bounded amount, so a whole playlist's worth of stale
        tracks stays competitive with a much weaker fresh pool — but a fresh
        track that was already close behind now wins outright.
        """
        ids = [add_track(db, f"t{i}", f"Artist {i}") for i in range(20)]
        assert _curate(db, ids, target=20, rng=random.Random(0))[0] == "t0"

        db.add(GeneratedPlaylist(
            name="Yesterday", track_ids=json.dumps(["t0"]),
            updated_at=datetime.utcnow() - timedelta(hours=6),
        ))
        db.commit()
        aged = _curate(db, ids, target=20, rng=random.Random(0))
        assert aged[0] != "t0", aged

    def test_deterministic_for_a_given_seed(self, db):
        ids = [add_track(db, f"t{i}", f"Artist {i % 4}") for i in range(40)]
        a = _curate(db, ids, target=20, rng=random.Random(9))
        b = _curate(db, ids, target=20, rng=random.Random(9))
        assert a == b

    def test_order_comes_from_the_blend_not_the_seed(self, db):
        """Blend position sets the base score, so the seed cannot reorder.

        Curation must never invent an order of its own: the blend already
        encodes which pools a track came from. The rng exists here only to
        break ties, and `_curate` produces none (every base score is unique),
        so two different seeds must agree exactly.
        """
        ids = [add_track(db, f"u{i}", f"Artist {i % 4}") for i in range(20)]
        first = _curate(db, ids, target=20, rng=random.Random(1))
        second = _curate(db, ids, target=20, rng=random.Random(2))
        assert first == second

    def test_metadata_failure_does_not_raise(self, db, monkeypatch):
        """A broken index must degrade to no curation, not a failed trigger."""
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(scheduler, "_track_meta", boom)
        ids = ["x", "y"]
        assert _curate(db, ids, target=50, rng=random.Random(0)) == ids


class TestPublishGate:
    def test_short_playlist_is_rejected(self):
        assert _publish_gate_ok(["t"] * (MIN_PLAYLIST_TRACKS - 1), "Mix") is False

    def test_exactly_minimum_is_accepted(self):
        assert _publish_gate_ok(["t"] * MIN_PLAYLIST_TRACKS, "Mix") is True

    def test_empty_playlist_is_rejected(self):
        assert _publish_gate_ok([], "Mix") is False
