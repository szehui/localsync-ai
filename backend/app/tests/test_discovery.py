"""Tests for the discovery feed service."""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Base,
    DiscoveryOffer,
    DiscoveryState,
    Track,
    TrackAnnotation,
    User,
)
from app.services import discovery
from app.services.sync import build_search_key

USER = 1
OTHER_USER = 2


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        User(id=USER, username="sze", navidrome_url="http://nav:4533",
             navidrome_password="secret", is_owner=True),
        User(id=OTHER_USER, username="guest", navidrome_url="http://nav:4533",
             navidrome_password="secret"),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.close()


def make_track(track_id, artist, title, mbid=None):
    return Track(
        id=track_id,
        title=title,
        artist_name=artist,
        recording_mbid=mbid,
        search_key=build_search_key(artist, title),
    )


def played(db, track_id, play_count=1, user_id=USER):
    """Record one user's plays for a track. Play counts are personal."""
    ann = TrackAnnotation(
        user_id=user_id, track_id=track_id, play_count=play_count)
    db.add(ann)
    return ann


def cand(mbid, artist, title, release=None, score=10.0, seed=("Seed", "Song")):
    return {
        "recording_mbid": mbid,
        "artist_name": artist,
        "track_title": title,
        "release_name": release,
        "release_mbid": None,
        "source": "labs",
        "score": score,
        "evidence": [{"seed_artist": seed[0], "seed_title": seed[1]}],
    }


class TestNormalizeArtist:
    def test_case_and_padding(self):
        assert discovery.normalize_artist("  EXO ") == "exo"

    def test_none_and_empty(self):
        assert discovery.normalize_artist(None) == ""
        assert discovery.normalize_artist("") == ""


class TestLibraryIndex:
    def test_collects_mbids_and_artists(self, db):
        db.add_all([
            make_track("s1", "Röyksopp", "Some Resolve", mbid="m1"),
            make_track("s2", "EXO", "Tempo", mbid=None),
            make_track("s3", "Exo", "Oasis", mbid="m2"),
        ])
        db.commit()
        mbids, artists = discovery.library_index(db)
        assert mbids == {"m1", "m2"}
        # 'EXO' and 'Exo' fold to one artist
        assert artists == {"röyksopp", "exo"}


class TestSelectSeeds:
    def test_one_seed_per_artist_by_play_count(self, db):
        db.add_all([
            make_track("s1", "EXO", "A", mbid="m1"),
            make_track("s2", "EXO", "B", mbid="m2"),
            make_track("s3", "Muse", "C", mbid="m3"),
        ])
        played(db, "s1", 100)
        played(db, "s2", 90)
        played(db, "s3", 50)
        db.commit()
        seeds = discovery.select_seeds(db, USER, limit=5)
        assert [s.id for s in seeds] == ["s1", "s3"]

    def test_requires_mbid_and_play_count(self, db):
        db.add_all([
            make_track("s1", "NoMbid", "A", mbid=None),
            make_track("s2", "NeverPlayed", "B", mbid="m2"),
            make_track("s3", "ZeroPlays", "C", mbid="m3"),
            make_track("s4", "Good", "D", mbid="m4"),
        ])
        played(db, "s1", 10)   # played, but untagged
        played(db, "s3", 0)    # annotated, never played
        played(db, "s4", 1)
        db.commit()
        seeds = discovery.select_seeds(db, USER, limit=5)
        assert [s.id for s in seeds] == ["s4"]

    def test_empty_library(self, db):
        assert discovery.select_seeds(db, USER, limit=5) == []

    def test_seeds_are_per_user(self, db):
        """A household favourite this user never played is not their seed."""
        db.add(make_track("s1", "Shared Artist", "Household Hit", mbid="m1"))
        played(db, "s1", 10, user_id=USER)
        played(db, "s1", 0, user_id=OTHER_USER)
        db.commit()

        assert [s.id for s in discovery.select_seeds(db, USER, limit=5)] == ["s1"]
        assert discovery.select_seeds(db, OTHER_USER, limit=5) == []

    def test_legacy_track_play_count_is_not_a_seed_source(self, db):
        """tracks.play_count is a dead household column; seeds come from annotations."""
        db.add(Track(id="s1", title="T", artist_name="A", recording_mbid="m1",
                     play_count=300, search_key="a|t"))
        db.commit()
        assert discovery.select_seeds(db, USER, limit=5) == []


class TestDedupeCandidates:
    def test_sums_score_and_merges_evidence(self):
        rows = [
            cand("m1", "A", "T", score=10, seed=("S1", "x")),
            cand("m1", "A", "T", score=5, seed=("S2", "y")),
        ]
        merged = discovery.dedupe_candidates(rows)
        assert len(merged) == 1
        assert merged[0]["score"] == 15
        assert len(merged[0]["evidence"]) == 2

    def test_drops_rows_without_mbid(self):
        assert discovery.dedupe_candidates([cand(None, "A", "T")]) == []

    def test_fills_metadata_gaps(self):
        rows = [
            {**cand("m1", None, None), "release_name": None},
            cand("m1", "Artist", "Title"),
        ]
        merged = discovery.dedupe_candidates(rows)
        assert merged[0]["artist_name"] == "Artist"
        assert merged[0]["track_title"] == "Title"


class TestTriageCandidates:
    def test_three_buckets(self):
        cands = [
            cand("owned", "Anything", "Owned Track"),
            cand("new1", "Absent Artist", "New"),
            cand("new2", "Known Artist", "Gap Fill"),
        ]
        owned, known, new = discovery.triage_candidates(
            cands, lib_mbids={"owned"}, lib_artists={"known artist"})
        assert [c["recording_mbid"] for c in owned] == ["owned"]
        assert [c["recording_mbid"] for c in known] == ["new2"]
        assert [c["recording_mbid"] for c in new] == ["new1"]

    def test_mbid_wins_over_artist_presence(self):
        """A recording we own is owned even if the artist name is unknown."""
        owned, known, new = discovery.triage_candidates(
            [cand("m1", "Unknown Artist", "T")], lib_mbids={"m1"}, lib_artists=set())
        assert len(owned) == 1
        assert known == [] and new == []

    def test_artist_match_is_case_insensitive(self):
        owned, known, new = discovery.triage_candidates(
            [cand("m9", "THE STROKES", "Reptilia")],
            lib_mbids=set(), lib_artists={"the strokes"})
        assert len(known) == 1

    def test_blank_artist_goes_to_new(self):
        owned, known, new = discovery.triage_candidates(
            [cand("m9", None, "T")], lib_mbids=set(), lib_artists={"x"})
        assert len(new) == 1


class TestCapPerArtist:
    def test_keeps_highest_scores(self):
        rows = [cand(f"m{i}", "Same", f"T{i}", score=float(i)) for i in range(6)]
        kept = discovery.cap_per_artist(rows, per_artist=2)
        assert len(kept) == 2
        assert {r["score"] for r in kept} == {4.0, 5.0}

    def test_drops_blank_artist(self):
        assert discovery.cap_per_artist([cand("m1", None, "T")]) == []


class TestBuildFeed:
    def _offer(self, mbid, artist, title, score=1.0, bucket="new_artist",
               status="offered", release="Rel"):
        return DiscoveryOffer(
            id=None, recording_mbid=mbid, artist_name=artist, track_title=title,
            release_name=release, source="labs", score=score, bucket=bucket,
            status=status, evidence=json.dumps([{"seed_artist": "Seed", "seed_title": title}]),
        )

    def test_groups_by_artist(self, db):
        offers = [
            self._offer("m1", "The Strokes", "Reptilia", score=5),
            self._offer("m2", "The Strokes", "12:51", score=3),
            self._offer("m3", "Pixies", "Where Is My Mind?", score=9),
        ]
        feed = discovery.build_feed(offers)
        assert len(feed) == 2
        strokes = next(g for g in feed if g["artist_name"] == "The Strokes")
        assert strokes["offer_count"] == 2
        assert strokes["score"] == 8
        # representative is the highest-scoring row
        assert strokes["representative_title"] == "Reptilia"

    def test_ranks_by_evidence_count_then_score(self):
        offers = [
            self._offer("m1", "FewEvidence", "A", score=100),
            self._offer("m2", "ManyEvidence", "B", score=1),
            self._offer("m3", "ManyEvidence", "C", score=1),
        ]
        feed = discovery.build_feed(offers)
        assert feed[0]["artist_name"] == "ManyEvidence"

    def test_limit_artists(self):
        offers = [self._offer(f"m{i}", f"Artist{i}", "T") for i in range(10)]
        assert len(discovery.build_feed(offers, limit_artists=3)) == 3


class TestUpsertOffers:
    def test_inserts_then_does_not_duplicate(self, db):
        added = discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)
        assert added == 1
        added = discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)
        assert added == 0
        assert db.query(DiscoveryOffer).count() == 1

    def test_dismissed_offer_never_resurrects(self, db):
        discovery.upsert_offers(db, USER, [cand("m1", "A", "T")],
                                discovery.NEW_ARTIST)
        offer = db.query(DiscoveryOffer).first()
        offer.status = discovery.STATUS_DISMISSED
        db.commit()

        discovery.upsert_offers(db, USER, [cand("m1", "A", "T")],
                                discovery.NEW_ARTIST)

        refreshed = db.query(DiscoveryOffer).first()
        assert refreshed.status == discovery.STATUS_DISMISSED
        assert discovery.offered_feed(db, USER) == []

    def test_same_recording_can_be_offered_to_two_users(self, db):
        """The old schema had recording_mbid UNIQUE, which made this impossible."""
        added = discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)
        added += discovery.upsert_offers(
            db, OTHER_USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)

        assert added == 2
        rows = db.query(DiscoveryOffer).filter_by(recording_mbid="m1").all()
        assert {r.user_id for r in rows} == {USER, OTHER_USER}

    def test_dismissed_offer_does_not_leak_to_other_users(self, db):
        """One person's dismissal is their opinion, not a household decision."""
        discovery.upsert_offers(db, USER, [cand("m1", "A", "T")],
                                discovery.NEW_ARTIST)
        mine = db.query(DiscoveryOffer).filter_by(user_id=USER).one()
        mine.status = discovery.STATUS_DISMISSED
        db.commit()

        added = discovery.upsert_offers(db, OTHER_USER, [cand("m1", "A", "T")],
                                        discovery.NEW_ARTIST)

        assert added == 1
        assert discovery.offered_feed(db, USER) == []
        assert [g["artist_name"] for g in discovery.offered_feed(db, OTHER_USER)] == ["A"]
        # And the dismisser's row is still dismissed, not overwritten.
        assert db.query(DiscoveryOffer).filter_by(user_id=USER).one().status == (
            discovery.STATUS_DISMISSED)

    def test_merges_evidence_across_runs(self, db):
        discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T", seed=("S1", "x"))], discovery.NEW_ARTIST)
        discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T", seed=("S2", "y"))], discovery.NEW_ARTIST)

        offer = db.query(DiscoveryOffer).first()
        evidence = json.loads(offer.evidence)
        assert len(evidence) == 2

    def test_bumps_score_to_max(self, db):
        discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T", score=5)], discovery.NEW_ARTIST)
        discovery.upsert_offers(
            db, USER, [cand("m1", "A", "T", score=99)], discovery.NEW_ARTIST)
        assert db.query(DiscoveryOffer).first().score == 99

    def test_deleting_a_user_removes_only_their_offers(self):
        """user_id is a NOT NULL FK with ON DELETE CASCADE, not a loose integer."""
        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False})

        # SQLite only honours ON DELETE CASCADE with this pragma per connection.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add_all([
                User(id=USER, username="sze", navidrome_url="http://nav:4533",
                     navidrome_password="secret", is_owner=True),
                User(id=OTHER_USER, username="guest",
                     navidrome_url="http://nav:4533", navidrome_password="secret"),
            ])
            session.commit()
            discovery.upsert_offers(
                session, USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)
            discovery.upsert_offers(
                session, OTHER_USER, [cand("m1", "A", "T")], discovery.NEW_ARTIST)

            session.query(User).filter_by(id=USER).delete()
            session.commit()

            assert session.query(DiscoveryOffer).filter_by(user_id=USER).count() == 0
            assert session.query(DiscoveryOffer).filter_by(
                user_id=OTHER_USER).count() == 1
        finally:
            session.close()


class FakeClient:
    """Minimal stand-in for ListenBrainzClient."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    async def get_similar_recordings(self, mbid, **kwargs):
        self.calls += 1
        return self.mapping.get(mbid, [])


def labs_row(mbid, artist, title, release="Rel", score=50):
    return {
        "recording_mbid": mbid,
        "artist_credit_name": artist,
        "recording_name": title,
        "release_name": release,
        "release_mbid": "rel-" + mbid,
        "score": score,
    }


class TestRefreshFeed:
    async def test_happy_path(self, db):
        db.add(make_track("s1", "The White Stripes", "Fell in Love", mbid="seed-1"))
        played(db, "s1", 3)
        db.commit()
        client = FakeClient({"seed-1": [
            labs_row("r1", "The Strokes", "Reptilia"),
            labs_row("r2", "Pixies", "Where Is My Mind?"),
            # Same recording MBID as the seed => already owned, must be dropped.
            labs_row("seed-1", "The White Stripes", "Fell in Love"),
        ]})

        summary = await discovery.refresh_feed(db, USER, client)

        assert summary["status"] == "ok"
        assert summary["offers_added"] == 2
        feed = discovery.offered_feed(db, USER)
        assert {g["artist_name"] for g in feed} == {"The Strokes", "Pixies"}
        state = db.query(DiscoveryState).first()
        assert state.last_status == "ok"

    async def test_known_artist_without_mbid_is_gap_fill_not_owned(self, db):
        """An unknown MBID on an artist we own is a gap-fill, not 'owned'."""
        db.add_all([
            make_track("s1", "Seed Artist", "Seed Song", mbid="seed-1"),
            make_track("s2", "The White Stripes", "Other", mbid="ws-1"),
        ])
        played(db, "s1", 2)
        played(db, "s2", 1)
        db.commit()
        client = FakeClient({"seed-1": [
            labs_row("unknown-mbid", "The White Stripes", "Seven Nation Army"),
        ]})

        summary = await discovery.refresh_feed(db, USER, client)

        assert summary["offers_added"] == 1
        offer = db.query(DiscoveryOffer).first()
        assert offer.bucket == discovery.ARTIST_KNOWN

    async def test_known_artist_goes_to_gap_fill_bucket(self, db):
        db.add_all([
            make_track("s1", "Seed Artist", "Seed Song", mbid="seed-1"),
            make_track("s2", "Bloc Party", "Banquet", mbid="other"),
        ])
        played(db, "s1", 2)
        played(db, "s2", 1)
        db.commit()
        client = FakeClient({"seed-1": [labs_row("r1", "Bloc Party", "Helicopter")]})

        await discovery.refresh_feed(db, USER, client)

        offer = db.query(DiscoveryOffer).first()
        assert offer.bucket == discovery.ARTIST_KNOWN

    async def test_uses_cache_and_makes_no_second_call(self, db):
        db.add(make_track("s1", "Seed Artist", "Seed Song", mbid="seed-1"))
        played(db, "s1", 1)
        db.commit()
        client = FakeClient({"seed-1": [labs_row("r1", "New", "T")]})

        first = await discovery.refresh_feed(db, USER, client)
        assert first["calls_made"] == 1

        second = await discovery.refresh_feed(db, USER, client)
        assert second["calls_made"] == 0  # 7d cache hit

    async def test_no_seeds_records_error_state(self, db):
        client = FakeClient({})
        summary = await discovery.refresh_feed(db, USER, client)
        assert summary["status"] == "error"
        state = db.query(DiscoveryState).first()
        assert state.last_status == "error"
        assert "no seeds" in (state.last_error or "").lower()

    async def test_one_bad_seed_does_not_abort(self, db):
        db.add_all([
            make_track("s1", "A", "a", mbid="seed-1"),
            make_track("s2", "B", "b", mbid="seed-2"),
        ])
        played(db, "s1", 2)
        played(db, "s2", 1)
        db.commit()

        class Exploding:
            calls = 0

            async def get_similar_recordings(self, mbid, **kwargs):
                Exploding.calls += 1
                if mbid == "seed-1":
                    raise RuntimeError("boom")
                return [labs_row("r9", "Good Artist", "Good Track")]

        summary = await discovery.refresh_feed(db, USER, Exploding())
        assert summary["status"] == "ok"
        assert summary["offers_added"] == 1

    async def test_refresh_only_persists_offers_for_the_calling_user(self, db):
        """One user's run must not populate, or fail because of, another's feed."""
        db.add(make_track("s1", "Seed Artist", "Seed Song", mbid="seed-1"))
        played(db, "s1", 3, user_id=USER)
        played(db, "s1", 0, user_id=OTHER_USER)
        db.commit()
        client = FakeClient({"seed-1": [labs_row("r1", "The Strokes", "Reptilia")]})

        first = await discovery.refresh_feed(db, USER, client)
        assert first["status"] == "ok"

        # The second user never played anything, so their run has no seeds —
        # the household's plays are not theirs to be seeded from.
        second = await discovery.refresh_feed(db, OTHER_USER, client)
        assert second["status"] == "error"

        assert db.query(DiscoveryOffer).filter_by(user_id=USER).count() == 1
        assert db.query(DiscoveryOffer).filter_by(user_id=OTHER_USER).count() == 0


class TestGatherEarlyStop:
    """Half of all MBIDs have no Labs neighbours, so scanning must be adaptive."""

    async def test_counts_live_seeds_and_skips_dead(self, db):
        seeds = [
            make_track(f"s{i}", f"Artist {i}", f"Song {i}", mbid=f"seed-{i}")
            for i in range(4)
        ]
        db.add_all(seeds)
        db.commit()

        client = FakeClient({
            "seed-0": [labs_row("r0", "New A", "Track A")],
            "seed-1": [],  # dead seed: MBID has no Labs neighbours
            "seed-2": [labs_row("r2", "New B", "Track B")],
            "seed-3": [],
        })

        candidates, calls, live, _scanned = await discovery.gather_labs_candidates(
            db, USER, client, seeds)

        assert calls == 4          # all four were uncached, so all were fetched
        assert live == 2           # only two contributed neighbours
        assert len(candidates) == 2

    async def test_stops_once_target_met_with_enough_live_seeds(self, db):
        # 20 seeds, each contributing 5 candidates => 50 candidates at 10 live.
        seeds = [
            make_track(f"s{i}", f"Artist {i}", f"Song {i}", mbid=f"seed-{i}")
            for i in range(20)
        ]
        db.add_all(seeds)
        db.commit()

        mapping = {
            f"seed-{i}": [labs_row(f"r{i}-{j}", f"New {i}", f"Track {i}-{j}") for j in range(5)]
            for i in range(20)
        }
        client = FakeClient(mapping)

        candidates, calls, live, _scanned = await discovery.gather_labs_candidates(
            db, USER, client, seeds, target_candidates=50, min_live_seeds=6,
        )

        assert len(candidates) >= 50
        assert live >= 6
        # Must not have burned all 20 seeds to get there.
        assert calls < 20

    async def test_respects_max_calls_cap(self, db):
        seeds = [
            make_track(f"s{i}", f"Artist {i}", f"Song {i}", mbid=f"seed-{i}")
            for i in range(10)
        ]
        db.add_all(seeds)
        db.commit()
        client = FakeClient({})

        _, calls, _, _scanned = await discovery.gather_labs_candidates(
            db, USER, client, seeds, max_calls=3,
        )
        assert calls == 3
