"""Tests for the playlist blend helper.

The blend is the part of the recency trigger most likely to silently misbehave
(short playlists, duplicated tracks, Last.fm crowding out ListenBrainz), so it
is extracted as a pure function and tested without any network or DB.
"""
import random

import pytest

from app.services.scheduler import _blend_tracks


def _ids(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


def _count(ids, prefix):
    return sum(1 for i in ids if str(i).startswith(prefix))


class TestBlendSplit:
    def test_returns_exact_target_when_pools_are_ample(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(1),
        )
        assert len(out) == 50

    def test_history_to_newest_split_is_60_40(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(1),
        )
        assert _count(out, "new") == 20
        assert _count(out, "lb") + _count(out, "nav") + _count(out, "lfm") == 30

    def test_listenbrainz_takes_60_percent_of_history(self):
        """LB replaces Last.fm as the primary history source."""
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(2),
        )
        assert _count(out, "lb") == 18
        assert _count(out, "nav") == 12
        assert _count(out, "lfm") == 0

    def test_lastfm_fills_when_listenbrainz_is_empty(self):
        """The whole point of the fallback: empty LB still yields a full mix."""
        out = _blend_tracks(
            lb_similar=[], nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(3),
        )
        assert len(out) == 50
        assert _count(out, "lb") == 0
        assert _count(out, "lfm") == 18
        assert _count(out, "nav") == 12

    def test_lastfm_used_only_to_fill_a_partial_lb_pool(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 5), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(4),
        )
        assert _count(out, "lb") == 5
        assert _count(out, "lfm") == 13   # 18 - 5
        assert _count(out, "nav") == 12
        assert len(out) == 50


class TestBlendIntegrity:
    def test_no_duplicates_in_output(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40),
            target=50, rng=random.Random(5),
        )
        assert len(out) == len(set(out))

    def test_track_shared_between_pools_appears_once(self):
        out = _blend_tracks(
            lb_similar=["shared"] + _ids("lb", 40),
            nav_similar=["shared"] + _ids("nav", 40),
            lfm_similar=[], newest_ids=["shared"] + _ids("new", 40),
            target=50, rng=random.Random(6),
        )
        assert out.count("shared") == 1

    def test_duplicates_within_one_pool_collapse(self):
        out = _blend_tracks(
            lb_similar=["dup", "dup", "dup"], nav_similar=[], lfm_similar=[],
            newest_ids=_ids("new", 40), target=50, rng=random.Random(7),
        )
        assert out.count("dup") == 1

    def test_never_exceeds_target(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 500), nav_similar=_ids("nav", 500),
            lfm_similar=_ids("lfm", 500), newest_ids=_ids("new", 500),
            target=50, rng=random.Random(8),
        )
        assert len(out) == 50

    def test_empty_everything_returns_empty(self):
        assert _blend_tracks([], [], [], [], target=50) == []


class TestBlendShortPools:
    def test_short_newest_pool_is_padded_from_history(self):
        """Shipping 35 tracks is worse than topping up from history."""
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 2),
            target=50, rng=random.Random(9),
        )
        assert len(out) == 50
        assert _count(out, "new") == 2

    def test_all_pools_short_returns_what_exists(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 3), nav_similar=_ids("nav", 2),
            lfm_similar=_ids("lfm", 1), newest_ids=_ids("new", 2),
            target=50, rng=random.Random(10),
        )
        assert len(out) == 8
        assert len(set(out)) == 8

    def test_only_newest_available(self):
        out = _blend_tracks(
            lb_similar=[], nav_similar=[], lfm_similar=[],
            newest_ids=_ids("new", 25), target=50, rng=random.Random(11),
        )
        assert len(out) == 25


class TestBlendDeterminism:
    def test_same_seed_same_order(self):
        kwargs = dict(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=_ids("lfm", 40), newest_ids=_ids("new", 40), target=50,
        )
        a = _blend_tracks(**kwargs, rng=random.Random(42))
        b = _blend_tracks(**kwargs, rng=random.Random(42))
        assert a == b

    def test_custom_share_knobs(self):
        out = _blend_tracks(
            lb_similar=_ids("lb", 40), nav_similar=_ids("nav", 40),
            lfm_similar=[], newest_ids=_ids("new", 40),
            target=20, newest_share=0.5, lb_share_of_history=1.0,
            rng=random.Random(12),
        )
        assert len(out) == 20
        assert _count(out, "new") == 10
        assert _count(out, "lb") == 10
