"""Tests for history-seeded, rotating discovery.

Covers the four things that made the feed stall at 255 offers for four days:
seeds that never came from listen history, a seed list that never rotated,
an ownership test running against half the library, and offers that never
aged out.
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Album,
    Base,
    DiscoveryOffer,
    DiscoverySeedUse,
    Track,
    TrackAnnotation,
    User,
)
from app.services import discovery

USER = 1
NOW = datetime(2026, 11, 28, 12, 0, 0)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    class OfflineNative:
        """The live owned-set read must degrade, not fail, when unreachable."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("navidrome unreachable")

    monkeypatch.setattr(discovery, "NavidromeNative", OfflineNative)
    session.add(User(id=USER, username="sze", navidrome_url="http://nav:4533",
                     navidrome_password="secret", is_owner=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def make_track(track_id, artist, title, mbid=None):
    return Track(id=track_id, title=title, artist_name=artist,
                 recording_mbid=mbid)


def played(db, track_id, play_count=1):
    db.add(TrackAnnotation(user_id=USER, track_id=track_id,
                           play_count=play_count))


def listen(artist, title, mbid, at=1_800_000_000):
    return {"artist": artist, "title": title, "recording_mbid": mbid,
            "listened_at": at, "album": "Some Album", "release_mbid": None,
            "artist_mbids": [], "source_client": "test"}


class FakeClient:
    """Stands in for ListenBrainzClient: history + Labs neighbours."""

    def __init__(self, listens=None, mapping=None):
        self.listens = listens or []
        self.mapping = mapping or {}
        self.calls = 0
        self.history_calls = 0

    async def get_recent_listens(self, days=90, count=1000):
        self.history_calls += 1
        return self.listens

    async def get_similar_recordings(self, mbid, **kwargs):
        self.calls += 1
        return self.mapping.get(mbid, [])


def labs_row(mbid, artist, title, release_mbid=None, score=50):
    return {
        "recording_mbid": mbid,
        "artist_credit_name": artist,
        "recording_name": title,
        "release_name": "Rel",
        "release_mbid": release_mbid or f"rel-{mbid}",
        "score": score,
    }


class TestListenbrainzSeeds:
    def test_one_seed_per_artist_keeping_the_most_recent(self):
        seeds = discovery.listenbrainz_seeds([
            listen("BLACKPINK", "Shut Down", "m1", at=100),
            listen("BLACKPINK", "How You Like That", "m2", at=200),
            listen("Jungle", "Casio", "m3", at=150),
        ])
        assert [s.recording_mbid for s in seeds] == ["m2", "m3"]
        assert all(s.source == "listenbrainz" for s in seeds)

    def test_orders_by_recency(self):
        seeds = discovery.listenbrainz_seeds([
            listen("A", "a", "m1", at=10),
            listen("B", "b", "m2", at=30),
            listen("C", "c", "m3", at=20),
        ])
        assert [s.artist_name for s in seeds] == ["B", "C", "A"]

    def test_drops_junk_artists(self):
        """An imported history is full of `Soundtrack` and blanks."""
        seeds = discovery.listenbrainz_seeds([
            listen("Soundtrack", "Theme", "m1"),
            listen("Various Artists", "Mix", "m2"),
            listen("", "No artist", "m3"),
            listen("Real Artist", "Real Song", "m4"),
        ])
        assert [s.artist_name for s in seeds] == ["Real Artist"]

    def test_requires_mbid_and_title(self):
        seeds = discovery.listenbrainz_seeds([
            listen("No MBID", "Song", None),
            listen("No Title", "", "m2"),
            listen("Kept", "Song", "m3"),
        ])
        assert [s.recording_mbid for s in seeds] == ["m3"]

    def test_respects_limit(self):
        listens = [listen(f"Artist {i}", "Song", f"m{i}") for i in range(20)]
        assert len(discovery.listenbrainz_seeds(listens, limit=5)) == 5


class TestMergeSeedPools:
    def lib(self, n):
        return [discovery.Seed(f"lib{i}", f"Lib Artist {i}", "Song", "library")
                for i in range(n)]

    def hist(self, n, offset=0):
        return [discovery.Seed(f"his{i}", f"Hist Artist {i}{offset}", "Song",
                               "listenbrainz") for i in range(n)]

    def test_history_artists_survive_a_full_library_pool(self):
        """The bug this quota exists to prevent: plays fill every slot."""
        merged = discovery.merge_seed_pools(self.lib(50), self.hist(20), limit=10)
        assert len(merged) == 10
        from_history = [s for s in merged if s.source == "listenbrainz"]
        assert len(from_history) == round(10 * discovery.LB_SEED_QUOTA_SHARE)

    def test_no_artist_appears_twice(self):
        library = [discovery.Seed("shared", "Same Artist", "Song", "library")]
        history = [discovery.Seed("shared2", "Same Artist", "Other", "listenbrainz")]
        merged = discovery.merge_seed_pools(library, history, limit=5)
        keys = [s.artist_key for s in merged]
        assert len(keys) == len(set(keys)) == 1

    def test_tops_up_from_history_when_plays_run_short(self):
        merged = discovery.merge_seed_pools(self.lib(3), self.hist(10), limit=10)
        assert len(merged) == 10

    def test_history_only_when_no_plays(self):
        merged = discovery.merge_seed_pools([], self.hist(4), limit=10)
        assert len(merged) == 4

    def test_empty_both(self):
        assert discovery.merge_seed_pools([], [], limit=10) == []


class TestRotation:
    def test_recently_used_seeds_move_to_the_back(self, db):
        seeds = [discovery.Seed(f"m{i}", f"Artist {i}", "Song", "library")
                 for i in range(3)]
        db.add(DiscoverySeedUse(user_id=USER, recording_mbid="m0",
                                used_at=NOW - timedelta(days=1)))
        db.commit()

        ordered = discovery.order_for_rotation(db, USER, seeds, now=NOW)
        assert [s.recording_mbid for s in ordered] == ["m1", "m2", "m0"]

    def test_rotated_seeds_are_deprioritised_not_dropped(self, db):
        """A thin pool must still fill the budget."""
        seeds = [discovery.Seed("m0", "Only Artist", "Song", "library")]
        db.add(DiscoverySeedUse(user_id=USER, recording_mbid="m0",
                                used_at=NOW - timedelta(days=1)))
        db.commit()

        assert len(discovery.order_for_rotation(db, USER, seeds, now=NOW)) == 1

    def test_old_uses_no_longer_rotate(self, db):
        seeds = [discovery.Seed("m0", "A", "Song", "library"),
                 discovery.Seed("m1", "B", "Song", "library")]
        db.add(DiscoverySeedUse(user_id=USER, recording_mbid="m0",
                                used_at=NOW - timedelta(days=30)))
        db.commit()

        ordered = discovery.order_for_rotation(db, USER, seeds, now=NOW)
        assert [s.recording_mbid for s in ordered] == ["m0", "m1"]

    def test_uses_are_per_user(self, db):
        db.add(DiscoverySeedUse(user_id=99, recording_mbid="m0", used_at=NOW))
        db.commit()
        seeds = [discovery.Seed("m0", "A", "Song", "library")]
        assert discovery.order_for_rotation(db, USER, seeds, now=NOW)[0].recording_mbid == "m0"


class TestRecordSeedUses:
    def test_records_then_updates_instead_of_duplicating(self, db):
        seeds = [discovery.Seed("m1", "A", "Song", "library")]

        discovery.record_seed_uses(db, USER, seeds, now=NOW)
        discovery.record_seed_uses(db, USER, seeds, now=NOW + timedelta(days=1))

        rows = db.query(DiscoverySeedUse).all()
        assert len(rows) == 1
        assert rows[0].used_at == NOW + timedelta(days=1)

    def test_empty_seed_list_is_a_noop(self, db):
        discovery.record_seed_uses(db, USER, [], now=NOW)
        assert db.query(DiscoverySeedUse).count() == 0


class TestExpireStaleOffers:
    def offer(self, db, mbid, status="offered", last_seen=NOW):
        db.add(DiscoveryOffer(
            user_id=USER, recording_mbid=mbid, artist_name=f"Artist {mbid}",
            bucket="new_artist", status=status, first_seen=last_seen,
            last_seen=last_seen,
        ))

    def test_expires_only_old_offered_rows(self, db):
        old = NOW - timedelta(days=discovery.OFFER_TTL_DAYS + 1)
        self.offer(db, "old", last_seen=old)
        self.offer(db, "fresh", last_seen=NOW - timedelta(days=1))
        self.offer(db, "dismissed-old", status="dismissed", last_seen=old)
        self.offer(db, "saved-old", status="saved", last_seen=old)
        db.commit()

        expired = discovery.expire_stale_offers(db, USER, now=NOW)

        assert expired == 1
        statuses = {o.recording_mbid: o.status
                    for o in db.query(DiscoveryOffer).all()}
        assert statuses["old"] == "expired"
        assert statuses["fresh"] == "offered"
        assert statuses["dismissed-old"] == "dismissed"
        assert statuses["saved-old"] == "saved"

    def test_expired_rows_leave_the_feed(self, db):
        self.offer(db, "old", last_seen=NOW - timedelta(days=200))
        db.commit()
        discovery.expire_stale_offers(db, USER, now=NOW)
        assert discovery.offered_rows(db, USER) == []


class TestOwnedCheck:
    def test_release_group_ownership_wins_over_an_unknown_recording_mbid(self):
        """A different edition of an album we hold is not a discovery."""
        candidates = [{
            "recording_mbid": "unknown-recording",
            "release_mbid": "owned-release-group",
            "artist_name": "Some Artist",
            "track_title": "Song",
        }]
        owned, known, new = discovery.triage_candidates(
            candidates, set(), {"some artist"}, {"owned-release-group"})
        assert owned == candidates and known == [] and new == []

    def test_release_group_check_is_optional(self):
        candidates = [{"recording_mbid": "r1", "release_mbid": "rg1",
                       "artist_name": "X", "track_title": "T"}]
        owned, known, new = discovery.triage_candidates(candidates, set(), set())
        assert owned == [] and new == candidates

    def test_mirror_release_groups_reads_the_album_table(self, db):
        db.add(Album(id="a1", name="Owned", release_group_mbid="rg-owned"))
        db.add(Album(id="a2", name="No MBID", release_group_mbid=None))
        db.commit()
        assert discovery.mirror_release_groups(db) == {"rg-owned"}


class TestOwnedReleaseGroups:
    async def test_unions_mirror_with_the_live_server(self, db, monkeypatch):
        """The mirror held 1,318 of 2,656 album MBIDs — the rest must come live."""
        db.add(Album(id="a1", name="In Mirror", release_group_mbid="rg-mirror"))
        db.commit()

        albums = [
            {"id": "s1", "name": "In Mirror", "mbzReleaseGroupId": "rg-mirror"},
            {"id": "s2", "name": "Mirror Missed This",
             "mbzReleaseGroupId": "rg-live-only"},
            {"id": "s3", "name": "Release Id Only", "mbzAlbumId": "rel-only"},
            {"id": "s4", "name": "No Tags"},
        ]

        class FakeNative:
            def __init__(self, *args, **kwargs):
                pass

            async def list_albums(self):
                return albums

            async def close(self):
                pass

        monkeypatch.setattr(discovery, "NavidromeNative", FakeNative)

        owned = await discovery.owned_release_groups(db, USER)

        assert {"rg-mirror", "rg-live-only", "rel-only"} <= owned

    async def test_falls_back_to_the_mirror_when_the_server_is_down(self, db):
        """The fixture's native stub always raises; the mirror must carry on."""
        db.add(Album(id="a1", name="In Mirror", release_group_mbid="rg-mirror"))
        db.commit()
        assert await discovery.owned_release_groups(db, USER) == {"rg-mirror"}

    async def test_unknown_user_yields_the_mirror(self, db):
        assert await discovery.owned_release_groups(db, 999) == set()


class TestRefreshFeedWithHistory:
    async def test_history_seeds_produce_offers_and_are_counted(self, db):
        # One played track, so the library pool exists but is small.
        db.add(make_track("t1", "Played Artist", "Played Song", "lib-mbid"))
        played(db, "t1", 2)
        db.commit()

        client = FakeClient(
            listens=[
                listen("Hist Artist", "Hist Song", "hist-mbid", at=1_800_000_000),
                listen("Soundtrack", "Junk", "junk-mbid", at=1_800_000_001),
            ],
            mapping={
                "lib-mbid": [labs_row("c1", "From Plays", "One")],
                "hist-mbid": [labs_row("c2", "From History", "Two")],
            },
        )

        summary = await discovery.refresh_feed(db, USER, client)

        assert summary["status"] == "ok"
        assert summary["lb_seeds_used"] == 1
        assert client.history_calls == 1
        feed = {g["artist_name"] for g in discovery.offered_feed(db, USER)}
        assert feed == {"From Plays", "From History"}

    async def test_history_evidence_names_the_seed_source(self, db):
        db.add(make_track("t1", "Played Artist", "Played Song", "lib-mbid"))
        played(db, "t1", 1)
        db.commit()
        client = FakeClient(
            listens=[listen("Hist Artist", "Hist Song", "hist-mbid")],
            mapping={"hist-mbid": [labs_row("c2", "From History", "Two")]},
        )

        await discovery.refresh_feed(db, USER, client)

        offer = db.query(DiscoveryOffer).filter_by(recording_mbid="c2").one()
        sources = {e.get("seed_source") for e in json.loads(offer.evidence)}
        assert sources == {"listenbrainz"}

    async def test_history_failure_still_produces_a_feed(self, db):
        """ListenBrainz is additive; a 429 must not empty the feed."""
        db.add(make_track("t1", "Played Artist", "Played Song", "lib-mbid"))
        played(db, "t1", 1)
        db.commit()

        class BrokenHistory(FakeClient):
            async def get_recent_listens(self, days=90, count=1000):
                raise RuntimeError("429 rate limited")

        client = BrokenHistory(
            mapping={"lib-mbid": [labs_row("c1", "From Plays", "One")]})

        summary = await discovery.refresh_feed(db, USER, client)

        assert summary["status"] == "ok"
        assert summary["lb_seeds_used"] == 0
        assert discovery.offered_feed(db, USER)

    async def test_client_without_history_method_still_works(self, db):
        db.add(make_track("t1", "Played Artist", "Played Song", "lib-mbid"))
        played(db, "t1", 1)
        db.commit()

        class LabsOnly:
            def __init__(self, mapping):
                self.mapping = mapping

            async def get_similar_recordings(self, mbid, **kwargs):
                return self.mapping.get(mbid, [])

        client = LabsOnly({"lib-mbid": [labs_row("c1", "From Plays", "One")]})
        summary = await discovery.refresh_feed(db, USER, client)

        assert summary["status"] == "ok"
        assert summary["lb_seeds_used"] == 0

    async def test_second_run_rotates_away_from_used_seeds(self, db):
        db.add(make_track("t1", "Artist A", "Song A", "a-mbid"))
        db.add(make_track("t2", "Artist B", "Song B", "b-mbid"))
        played(db, "t1", 5)
        played(db, "t2", 4)
        db.commit()

        client = FakeClient(mapping={
            "a-mbid": [labs_row("ra", "Result A", "One")],
            "b-mbid": [labs_row("rb", "Result B", "Two")],
        })

        await discovery.refresh_feed(db, USER, client, seed_count=1)
        first = {r.recording_mbid for r in db.query(DiscoverySeedUse).all()}
        assert first == {"a-mbid"}

        await discovery.refresh_feed(db, USER, client, seed_count=1)
        # The seed used by the first run is deprioritised, so the second run
        # explores the other artist and records it too.
        assert {r.recording_mbid for r in db.query(DiscoverySeedUse).all()} == {
            "a-mbid", "b-mbid"}

    async def test_state_records_history_and_expiry_counts(self, db):
        db.add(make_track("t1", "Played Artist", "Played Song", "lib-mbid"))
        played(db, "t1", 1)
        db.add(DiscoveryOffer(
            user_id=USER, recording_mbid="stale", artist_name="Old",
            bucket="new_artist", status="offered",
            first_seen=NOW - timedelta(days=100),
            last_seen=NOW - timedelta(days=100),
        ))
        db.commit()

        client = FakeClient(listens=[listen("Hist", "Song", "hist-mbid")],
                            mapping={"hist-mbid": [labs_row("c2", "New", "Two")]})
        await discovery.refresh_feed(db, USER, client)

        from app.models.database import DiscoveryState
        state = db.query(DiscoveryState).filter_by(user_id=USER).one()
        assert state.lb_seeds_used == 1
        assert state.offers_expired == 1
