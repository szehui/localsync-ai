"""Tests for the library-resident mixes.

Unlike the curation primitives these need a database, so they run against a
real in-memory SQLite instance with the actual `Track` model — not a MagicMock.
The queries (group-by, window filters, `IN`, the annotation outer join) are the
whole point of this module, so mocking them out would test nothing.

Play state is per user (`track_annotations`), so the helper below writes an
annotation rather than the legacy `Track.play_count` column: the mixes must
read the former and ignore the latter.
"""
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Track, TrackAnnotation
from app.services import mixes
from app.services.mixes import (
    MIX_SPECS,
    select_deep_cuts,
    select_mix,
    select_on_repeat,
    select_rediscover,
    select_rewind,
    select_unplayed,
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


def add_track(db, track_id, artist, plays=0, genre="rock", last_played_days=None,
              user_id=1, annotate=True):
    """Create a library track and, unless `annotate=False`, one user's annotation.

    `annotate=False` models a track nobody has ever played — the row that has no
    annotation at all and must still be visible to the Unplayed mix.
    """
    track = db.merge(Track(
        id=track_id,
        title=f"Title {track_id}",
        artist_name=artist,
        genre=genre,
    ))
    db.flush()
    if annotate:
        db.merge(TrackAnnotation(
            user_id=user_id,
            track_id=track_id,
            play_count=plays,
            last_played_at=(
                None if last_played_days is None
                else datetime.utcnow() - timedelta(days=last_played_days)
            ),
        ))
        db.flush()
    return track


class TestDeepCuts:
    def test_skips_each_artists_most_played_tracks(self, db):
        """Aurral's rule: skip the top 5 of an artist's catalog, take from below."""
        for i in range(10):
            add_track(db, f"a{i}", "Artist A", plays=100 - i)
        out = select_deep_cuts(db, 1, limit=10, top_artists=1, per_artist=3, skip_top=5)
        ids = {e["id"] for e in out}
        assert len(ids) == 3
        # The 5 most played (a0..a4) are the "hits" and must be absent.
        assert not ids & {f"a{i}" for i in range(5)}
        assert ids <= {f"a{i}" for i in range(5, 10)}

    def test_artist_without_deep_tracks_contributes_nothing(self, db):
        for i in range(3):
            add_track(db, f"a{i}", "Artist A", plays=50 - i)
        assert select_deep_cuts(db, 1, top_artists=1, per_artist=3, skip_top=5) == []

    def test_unplayed_tracks_are_not_deep_cuts(self, db):
        for i in range(8):
            add_track(db, f"a{i}", "Artist A", plays=10 - i)
        for i in range(4):
            add_track(db, f"z{i}", "Artist A", plays=0)
        out = select_deep_cuts(db, 1, top_artists=1, per_artist=5, skip_top=2)
        assert not {e["id"] for e in out} & {"z0", "z1", "z2", "z3"}

    def test_tracks_without_any_annotation_are_not_deep_cuts(self, db):
        """No annotation row means no plays: not a deep cut either."""
        for i in range(8):
            add_track(db, f"a{i}", "Artist A", plays=10 - i)
        for i in range(4):
            add_track(db, f"z{i}", "Artist A", annotate=False)
        out = select_deep_cuts(db, 1, top_artists=1, per_artist=5, skip_top=2)
        assert not {e["id"] for e in out} & {"z0", "z1", "z2", "z3"}

    def test_limits_artist_count(self, db):
        for a in range(5):
            for i in range(10):
                add_track(db, f"a{a}_{i}", f"Artist {a}", plays=100 - i)
        out = select_deep_cuts(db, 1, limit=50, top_artists=2, per_artist=3, skip_top=2)
        assert len({e["artist"] for e in out}) == 2

    def test_output_is_interleaved_across_artists(self, db):
        for a in range(3):
            for i in range(10):
                add_track(db, f"a{a}_{i}", f"Artist {a}", plays=100 - i)
        out = select_deep_cuts(db, 1, limit=9, top_artists=3, per_artist=3, skip_top=2)
        artists = [e["artist"] for e in out]
        assert artists[0] != artists[1], f"one artist's batch was not broken up: {artists}"

    def test_deterministic_for_a_given_seed(self, db):
        for i in range(20):
            add_track(db, f"a{i}", "Artist A", plays=50 - i)
        first = select_deep_cuts(db, 1, top_artists=1, per_artist=3, skip_top=2,
                                 rng=random.Random(5))
        second = select_deep_cuts(db, 1, top_artists=1, per_artist=3, skip_top=2,
                                  rng=random.Random(5))
        assert [e["id"] for e in first] == [e["id"] for e in second]


class TestTimeWindowMixes:
    def test_on_repeat_is_recent_heavy_rotation(self, db):
        add_track(db, "recent", "A", plays=20, last_played_days=3)
        add_track(db, "stale", "B", plays=20, last_played_days=200)
        add_track(db, "rare", "C", plays=1, last_played_days=3)
        ids = {e["id"] for e in select_on_repeat(db, 1, min_plays=5, within_days=30)}
        assert ids == {"recent"}

    def test_rewind_excludes_the_last_month(self, db):
        """Rewind is \"earlier this year\", so this month's rotation is excluded."""
        add_track(db, "this_month", "A", plays=20, last_played_days=5)
        add_track(db, "spring", "B", plays=20, last_played_days=100)
        add_track(db, "last_year", "C", plays=20, last_played_days=500)
        ids = {e["id"] for e in select_rewind(db, 1, min_plays=5, older_than_days=30,
                                              within_days=365)}
        assert ids == {"spring"}

    def test_rediscover_is_untouched_for_a_year(self, db):
        add_track(db, "recent", "A", plays=20, last_played_days=10)
        add_track(db, "old", "B", plays=10, last_played_days=400)
        ids = {e["id"] for e in select_rediscover(db, 1, min_plays=3,
                                                  older_than_days=365)}
        assert ids == {"old"}

    def test_unplayed_timestamps_are_excluded(self, db):
        """A track with no play history is not evidence of anything."""
        add_track(db, "never_played", "A", plays=0, last_played_days=None)
        assert select_on_repeat(db, 1, min_plays=0) == []

    def test_empty_result_when_last_played_is_not_populated(self, db):
        """The annotation can exist with a NULL last-played until a sync fills it."""
        add_track(db, "a", "A", plays=10)
        assert select_on_repeat(db, 1) == []
        assert select_rewind(db, 1) == []
        assert select_rediscover(db, 1) == []

    def test_min_plays_is_respected(self, db):
        add_track(db, "one", "A", plays=1, last_played_days=3)
        add_track(db, "many", "B", plays=50, last_played_days=3)
        ids = {e["id"] for e in select_on_repeat(db, 1, min_plays=5, within_days=30)}
        assert ids == {"many"}

    def test_score_comes_from_the_annotation(self, db):
        """The dead Track.play_count column must not drive the score."""
        add_track(db, "a", "A", plays=7, last_played_days=3)
        (entry,) = select_on_repeat(db, 1, min_plays=5, within_days=30)
        assert entry["score"] == 7.0


class TestUnplayed:
    def test_gated_on_having_real_play_history(self, db):
        """With almost no plays, \"unplayed\" is just \"the library\" — noise."""
        add_track(db, "played", "A", plays=3)
        for i in range(10):
            add_track(db, f"u{i}", "B", plays=0)
        assert select_unplayed(db, 1, min_played_library=50) == []

    def test_only_artists_already_in_rotation(self, db):
        for i in range(60):
            add_track(db, f"p{i}", "Loved", plays=5)
        add_track(db, "loved_unplayed", "Loved", plays=0)
        add_track(db, "stranger", "Never Played Artist", plays=0)
        ids = {e["id"] for e in select_unplayed(db, 1, min_played_library=50,
                                                rng=random.Random(0))}
        assert "loved_unplayed" in ids
        assert "stranger" not in ids

    def test_respects_limit(self, db):
        for i in range(60):
            add_track(db, f"p{i}", "Loved", plays=5)
        for i in range(30):
            add_track(db, f"u{i}", "Loved", plays=0)
        out = select_unplayed(db, 1, limit=10, min_played_library=50,
                              rng=random.Random(0))
        assert len(out) == 10

    def test_unplayed_still_sees_tracks_with_no_annotation_row(self, db):
        """LEFT JOIN proof: never-annotated tracks stay eligible for Unplayed.

        An inner join would hide them — the library would silently shrink to
        only what this user has already played.
        """
        for i in range(60):
            add_track(db, f"p{i}", "Loved", plays=5)
        add_track(db, "never_touched", "Loved", annotate=False)
        ids = {e["id"] for e in select_unplayed(db, 1, min_played_library=50,
                                                rng=random.Random(0))}
        assert "never_touched" in ids


class TestPerUserIsolation:
    def test_mixes_are_isolated_per_user(self, db):
        """Same track, two users: user 1's plays must not leak into user 2's mix."""
        add_track(db, "t1", "A", plays=10, last_played_days=3, user_id=1)
        add_track(db, "t1", "A", plays=0, last_played_days=3, user_id=2)

        assert {e["id"] for e in select_on_repeat(db, 1)} == {"t1"}
        assert select_on_repeat(db, 2) == []

    def test_unplayed_is_isolated_per_user(self, db):
        """A track user 1 has played is still unplayed for user 2."""
        for i in range(60):
            add_track(db, f"p{i}", "Loved", plays=5, user_id=1)
            add_track(db, f"p{i}", "Loved", plays=5, user_id=2)
        add_track(db, "mine", "Loved", plays=9, last_played_days=3, user_id=1)
        add_track(db, "mine", "Loved", plays=0, user_id=2)

        ids_1 = {e["id"] for e in select_unplayed(db, 1, min_played_library=50,
                                                  rng=random.Random(0))}
        ids_2 = {e["id"] for e in select_unplayed(db, 2, min_played_library=50,
                                                  rng=random.Random(0))}
        assert "mine" not in ids_1
        assert "mine" in ids_2


class TestSelectMixDispatch:
    def test_every_spec_has_a_selector(self):
        for kind in MIX_SPECS:
            assert kind in mixes._SELECTORS, f"{kind} has no selector"

    def test_unknown_kind_falls_back(self, db, caplog):
        add_track(db, "a", "A", plays=10)
        assert select_mix(db, 1, "no_such_mix") == []
        assert "falling back" in caplog.text.lower()

    def test_spec_limit_is_applied(self, db):
        for i in range(60):
            add_track(db, f"p{i}", f"Artist {i % 5}", plays=20, last_played_days=3)
        out = select_mix(db, 1, "on_repeat", rng=random.Random(0))
        assert len(out) <= MIX_SPECS["on_repeat"]["limit"]

    def test_explicit_limit_is_honoured(self, db):
        """A caller-supplied limit must not collide with the spec's default.

        Regression: `target = int(limit or spec.pop("limit", 50))` short-circuits
        when `limit` is truthy, so "limit" stayed in `spec` and was passed to the
        selector twice -> TypeError. Every call site currently omits `limit`, so
        nothing caught it; the first `?limit=` API parameter would have.
        """
        for i in range(60):
            add_track(db, f"p{i}", f"Artist {i % 5}", plays=20, last_played_days=3)
        out = select_mix(db, 1, "on_repeat", limit=7, rng=random.Random(0))
        assert len(out) == 7
        # And it must still beat the spec default rather than being ignored.
        assert len(select_mix(db, 1, "on_repeat", limit=3, rng=random.Random(0))) == 3

    def test_user_id_is_forwarded(self, db):
        add_track(db, "t1", "A", plays=20, last_played_days=3, user_id=1)
        assert {e["id"] for e in select_mix(db, 1, "on_repeat")} == {"t1"}
        assert select_mix(db, 2, "on_repeat") == []

    def test_result_shape(self, db):
        for i in range(10):
            add_track(db, f"a{i}", "Artist A", plays=20 - i, genre="shoegaze")
        out = select_deep_cuts(db, 1, top_artists=1, per_artist=2, skip_top=1)
        assert out
        entry = out[0]
        assert set(entry) == {"id", "artist", "genre", "score", "reason"}
        assert entry["artist"] == "Artist A"
        assert entry["genre"] == "shoegaze"
        assert entry["reason"].startswith("deep_cut")
