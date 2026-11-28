"""Tests for the three-tier MBID backfill.

The matching rules here are not stylistic choices — each one is a measured
regression from the Phase 0 probe on the real library, and tightening them
demonstrably loses correct matches. The tests say so at the point where it
matters.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Album, Base
from app.services.mbid_backfill import (
    candidate_tier,
    oracle_tier,
    pick_best,
    report,
    tail_tier,
    untagged,
)

MBID_A = "aaaaaaaa-0000-0000-0000-000000000001"
MBID_B = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def local(name, artist, year=None, tracks=10):
    return {"id": "al1", "name": name, "artist": artist, "year": year, "tracks": tracks}


def rg(title, artist, *, mbid=MBID_A, primary="Album", secondary=None, date=None,
      score=100):
    return {
        "id": mbid,
        "title": title,
        "primary-type": primary,
        "secondary-types": secondary or [],
        "first-release-date": date,
        "score": score,
        "artist-credit": [{"name": artist, "artist": {"id": "artist-1"}}],
    }


class TestCandidateTier:
    def test_exact_title_and_artist_is_high(self):
        tier, _ = candidate_tier(local("Hysteria", "Def Leppard"),
                                 rg("Hysteria", "Def Leppard"))
        assert tier == "high"

    def test_extended_mix_still_exact(self):
        # An apostrophe/ampersand-class difference must not break identity.
        tier, _ = candidate_tier(local("Fire and Fury", "Sam & Dave"),
                                 rg("Fire & Fury", "Sam and Dave"))
        assert tier == "high"

    def test_edition_phrase_outside_brackets_is_not_stripped(self):
        # Packaging text outside brackets ("25th Anniversary Edition") is not
        # removed by norm_title (brackets only) or loose_title (brackets only),
        # so these genuinely differ — tier is None, not a loose match.
        assert candidate_tier(local("My Way 25th Anniversary Edition", "Usher"),
                              rg("My Way", "Usher"))[0] is None

    def test_unknown_bracket_is_low_not_rejected(self):
        # "1989 [nightcore]" keeps its bracket under norm_title ("nightcore"
        # is not packaging), but loose_title drops every bracket → "low"
        # tier: a confirmation-only match, never accepted alone.
        assert candidate_tier(local("1989", "Taylor Swift"),
                              rg("1989 [nightcore]", "Taylor Swift"))[0] == "low"

    def test_leading_article_is_loose_not_rejected(self):
        # U2 — "Joshua Tree" vs MusicBrainz "The Joshua Tree" (leading article)
        tier, reasons = candidate_tier(local("Joshua Tree", "U2"),
                                       rg("The Joshua Tree", "U2"))
        assert tier == "low"
        assert any("article" in r for r in reasons)

    def test_artist_variant_is_medium(self):
        # Pharrell vs Pharrell Williams (measured in the oracle validation).
        tier, reasons = candidate_tier(local("In My Mind", "Pharrell Williams"),
                                       rg("In My Mind", "Pharrell"))
        assert tier == "medium"
        assert any("artist" in r for r in reasons)

    def test_different_artist_rejected(self):
        assert candidate_tier(local("Hysteria", "Def Leppard"),
                              rg("Hysteria", "Muse"))[0] is None

    def test_different_title_rejected(self):
        assert candidate_tier(local("Adrenalize", "Def Leppard"),
                              rg("Hysteria", "Def Leppard"))[0] is None

    def test_interview_rejected(self):
        assert candidate_tier(local("Hysteria", "Def Leppard"),
                              rg("Hysteria", "Def Leppard",
                                 secondary=["Interview"]))[0] is None

    def test_live_is_requestable(self):
        # "Live" is not a junk secondary type: a live album is still requestable
        # in Lidarr, so it matches at high tier.
        cand = rg("Hysteria", "Def Leppard", secondary=["Live"])
        assert candidate_tier(local("Hysteria", "Def Leppard"), cand)[0] == "high"

    def test_single_rejected(self):
        assert candidate_tier(local("Hysteria", "Def Leppard"),
                              rg("Hysteria", "Def Leppard", primary="Single"))[0] is None

    def test_year_does_not_veto(self):
        # Run DMC — Raising Hell: local maxYear 1999 vs release group 1986.
        tier, _ = candidate_tier(local("Raising Hell", "Run DMC", year=1999),
                                 rg("Raising Hell", "Run DMC", date="1986-05-15"))
        assert tier == "high"

    def test_track_count_is_not_even_consulted(self):
        # Notorious B.I.G. — Doing It Real Big: 4 local tracks vs 14 on the RG.
        tier, _ = candidate_tier(local("Doing It Real Big", "Notorious B.I.G.", tracks=4),
                                 rg("Doing It Real Big", "Notorious B.I.G.", date="2015"))
        assert tier == "high"


class TestPickBest:
    def test_prefers_high_over_medium(self):
        best, tier, distinct = pick_best(
            local("In My Mind", "Pharrell Williams"),
            [rg("In My Mind", "Pharrell"), rg("In My Mind", "Pharrell Williams")],
        )
        assert tier == "high"
        assert best["artist-credit"][0]["name"] == "Pharrell Williams"

    def test_year_breaks_a_tie(self):
        best, _, _ = pick_best(
            local("Greatest Hits", "Queen", year=1981),
            [rg("Greatest Hits", "Queen", mbid=MBID_A, date="1992"),
             rg("Greatest Hits", "Queen", mbid=MBID_B, date="1981")],
        )
        assert best["id"] == MBID_B

    def test_counts_distinct_candidates(self):
        _, _, distinct = pick_best(
            local("Greatest Hits", "Queen"),
            [rg("Greatest Hits", "Queen", mbid=MBID_A),
             rg("Greatest Hits", "Queen", mbid=MBID_B)],
        )
        assert distinct == 2

    def test_no_match_returns_none(self):
        best, tier, distinct = pick_best(local("Nope", "Nobody"),
                                         [rg("Other", "Somebody")])
        assert best is None and tier is None and distinct == 0


class TestOracleTier:
    def _lidarr(self, title, artist, mbid):
        return {"foreignAlbumId": mbid, "title": title,
                "artist": {"artistName": artist}}

    def test_copies_a_unique_match(self, db):
        db.add(Album(id="al1", name="Random Access Memories", artist_name="Daft Punk"))
        db.commit()
        stats = oracle_tier(db, [self._lidarr("Random Access Memories",
                                              "Daft Punk", MBID_A)])
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid == MBID_A
        assert album.mbid_source == "lidarr_oracle"
        assert album.mbid_confidence == "high"
        assert stats["resolved"] == 1

    def test_edition_variant_matches_highly(self, db):
        # "(Deluxe Edition)" is packaging → stripped by norm_title → exact → high.
        db.add(Album(id="al1", name="Hysteria (Deluxe Edition)",
                     artist_name="Def Leppard"))
        db.commit()
        oracle_tier(db, [self._lidarr("Hysteria", "Def Leppard", MBID_A)])
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid == MBID_A
        assert album.mbid_confidence == "high"

    def test_ambiguous_key_is_skipped_not_guessed(self, db):
        db.add(Album(id="al1", name="Greatest Hits", artist_name="Queen"))
        db.commit()
        stats = oracle_tier(db, [
            self._lidarr("Greatest Hits", "Queen", MBID_A),
            self._lidarr("Greatest Hits", "Queen", MBID_B),
        ])
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid is None
        assert album.mbid_checked_at is None   # left for the tail tier
        assert stats["ambiguous"] == 1

    def test_unmatched_stays_unchecked(self, db):
        db.add(Album(id="al1", name="Obscure Album", artist_name="Nobody"))
        db.commit()
        stats = oracle_tier(db, [self._lidarr("Other", "Someone", MBID_A)])
        album = db.query(Album).filter_by(id="al1").one()
        assert album.mbid_checked_at is None
        assert stats["unmatched"] == 1

    def test_already_tagged_albums_are_untouched(self, db):
        db.add(Album(id="al1", name="Album", artist_name="Artist",
                     release_group_mbid="existing"))
        db.commit()
        oracle_tier(db, [self._lidarr("Album", "Artist", MBID_A)])
        assert db.query(Album).filter_by(id="al1").one().release_group_mbid == "existing"

    def test_rows_without_foreign_id_are_ignored(self, db):
        db.add(Album(id="al1", name="Album", artist_name="Artist"))
        db.commit()
        oracle_tier(db, [{"foreignAlbumId": None, "title": "Album",
                          "artist": {"artistName": "Artist"}}])
        assert db.query(Album).filter_by(id="al1").one().release_group_mbid is None


class FakeMB:
    """Minimal stand-in returning scripted artist and discography data."""

    def __init__(self, artists=None, browse=None):
        self.artists = artists or []
        self.browse = browse or {}
        self.browse_calls = []
        self.browsed_artists = []

    async def search_artists(self, query, limit=5):
        return self.artists

    async def browse_artist_release_groups(self, artist_mbid, *, type_="album",
                                           limit=100, offset=0):
        self.browse_calls.append(type_)
        self.browsed_artists.append(artist_mbid)
        return self.browse.get(type_, [])


class TestTailTier:
    async def test_resolves_via_discography_browse(self, db):
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard",
                     song_count=12))
        db.commit()
        mb = FakeMB(
            artists=[{"id": "artist-1", "name": "Def Leppard"}],
            browse={"album": [rg("Hysteria", "Def Leppard")]},
        )
        stats = await tail_tier(db, mb)
        album = db.query(Album).filter_by(id="al1").one()
        assert album.release_group_mbid == MBID_A
        assert album.mbid_source == "musicbrainz_backfill"
        assert stats["resolved"] == 1

    async def test_browses_album_and_ep_separately(self, db):
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard",
                     song_count=12))
        db.commit()
        mb = FakeMB(artists=[{"id": "artist-1", "name": "Def Leppard"}],
                    browse={"album": [rg("Hysteria", "Def Leppard")]})
        await tail_tier(db, mb)
        assert mb.browse_calls == ["album", "ep"]

    async def test_singles_are_skipped_and_marked(self, db):
        db.add(Album(id="al1", name="A Single", artist_name="Someone",
                     song_count=1))
        db.commit()
        mb = FakeMB(artists=[])
        stats = await tail_tier(db, mb)
        album = db.query(Album).filter_by(id="al1").one()
        assert stats["skipped_singles"] == 1
        assert album.mbid_checked_at is not None
        assert album.release_group_mbid is None
        assert mb.browse_calls == []

    async def test_unresolved_artist_is_marked(self, db):
        db.add(Album(id="al1", name="八度空间", artist_name="Jay Chou",
                     song_count=10))
        db.commit()
        mb = FakeMB(artists=[{"id": "artist-x", "name": "Chou"}])
        stats = await tail_tier(db, mb)
        album = db.query(Album).filter_by(id="al1").one()
        assert stats["artist_unresolved"] == 1
        assert album.mbid_checked_at is not None

    async def test_first_search_result_is_not_taken_blindly(self, db):
        # MusicBrainz returns partial-name matches first. A "Various Artists"
        # tribute entry must not be used just because it ranked first.
        db.add(Album(id="al1", name="Dark Blood", artist_name="ENHYPEN",
                     song_count=6))
        db.commit()
        mb = FakeMB(
            artists=[{"id": "wrong", "name": "ENHYPEN Tribute Band"},
                     {"id": "right", "name": "ENHYPEN"}],
            browse={"album": [rg("Dark Blood", "ENHYPEN")]},
        )
        await tail_tier(db, mb)
        assert mb.browsed_artists == ["right", "right"]

    async def test_no_title_match_is_marked(self, db):
        db.add(Album(id="al1", name="Something Else", artist_name="Def Leppard",
                     song_count=10))
        db.commit()
        mb = FakeMB(artists=[{"id": "artist-1", "name": "Def Leppard"}],
                    browse={"album": [rg("Hysteria", "Def Leppard")]})
        stats = await tail_tier(db, mb)
        album = db.query(Album).filter_by(id="al1").one()
        assert stats["unmatched"] == 1
        assert album.mbid_checked_at is not None

    async def test_already_checked_albums_are_not_revisited(self, db):
        from datetime import datetime
        db.add(Album(id="al1", name="Hysteria", artist_name="Def Leppard",
                     song_count=12, mbid_checked_at=datetime(2026, 1, 1)))
        db.commit()
        mb = FakeMB(artists=[{"id": "artist-1", "name": "Def Leppard"}],
                    browse={"album": [rg("Hysteria", "Def Leppard")]})
        stats = await tail_tier(db, mb)
        assert stats["albums"] == 0
        assert mb.browse_calls == []


class TestReport:
    def test_counts_by_source(self, db):
        db.add_all([
            Album(id="a", name="A", mbid_source="lidarr_oracle", release_group_mbid=MBID_A),
            Album(id="b", name="B", mbid_source="navidrome_tag", release_group_mbid=MBID_B),
            Album(id="c", name="C"),
        ])
        db.commit()
        out = report(db)
        assert out["albums"] == 3
        assert out["tagged"] == 2
        assert out["by_source"] == {"lidarr_oracle": 1, "navidrome_tag": 1}
        assert out["never_checked"] == 1

    def test_coverage_rounds(self, db):
        db.add(Album(id="a", name="A", release_group_mbid=MBID_A))
        db.commit()
        assert report(db)["coverage"] == 1.0

    def test_empty_library_does_not_divide_by_zero(self, db):
        assert report(db)["coverage"] == 0.0


class TestUntagged:
    def test_excludes_tagged_and_already_checked(self, db):
        from datetime import datetime
        db.add_all([
            Album(id="a", name="A", release_group_mbid=MBID_A),
            Album(id="b", name="B", mbid_checked_at=datetime(2026, 1, 1)),
            Album(id="c", name="C"),
        ])
        db.commit()
        assert [a.id for a in untagged(db)] == ["c"]

    def test_retry_failed_includes_checked(self, db):
        from datetime import datetime
        db.add(Album(id="b", name="B", mbid_checked_at=datetime(2026, 1, 1)))
        db.commit()
        assert [a.id for a in untagged(db, retry_failed=True)] == ["b"]
