"""Tests for text normalisation.

Each case here comes from a real album in the library that the Phase 0 probe got
wrong, so these are regression tests for measured failures rather than invented
edge cases.
"""
from app.services.title_norm import (
    album_key,
    fold,
    has_cjk,
    loose_album_key,
    loose_title,
    norm_artist,
    norm_title,
    strip_packaging,
    strip_punctuation,
    without_article,
)


class TestFold:
    def test_case_and_diacritics_ignored(self):
        assert fold("Röyksopp") == fold("royksopp")

    def test_ampersand_becomes_and(self):
        assert fold("Hall & Oates") == fold("Hall and Oates")

    def test_punctuation_squashed(self):
        assert fold("ATUM: A Rock Opera") == fold("ATUM A Rock Opera")

    def test_cjk_preserved(self):
        assert fold("싸다9") == "싸다9"

    def test_empty_is_empty(self):
        assert fold("") == ""
        assert fold(None) == ""


class TestNormTitle:
    def test_deluxe_bracket_dropped(self):
        assert norm_title("Random Access Memories (Deluxe Edition)") == "random access memories"

    def test_remaster_bracket_dropped(self):
        assert norm_title("Hysteria [Remastered]") == "hysteria"

    def test_unknown_bracket_kept(self):
        # The regression from Phase 0: stripping every bracket collapsed
        # "1989 [nightcore]" onto "1989" and accepted the wrong release group.
        assert norm_title("1989 [nightcore]") != norm_title("1989")

    def test_disc_suffix_dropped(self):
        assert norm_title("The Fame Monster (CD 2)") == norm_title("The Fame Monster")

    def test_act_suffix_dropped(self):
        assert norm_title("ATUM - Act 1") == "atum"

    def test_ep_suffix_dropped(self):
        assert norm_title("Positions - EP") == "positions"

    def test_non_latin_kept(self):
        assert norm_title("화양연화 pt.1") == "화양연화 pt 1"

    def test_ampersand_variant_equates(self):
        assert norm_title("Fire & Fury") == norm_title("Fire and Fury")


class TestLooseTitle:
    def test_every_bracket_dropped(self):
        assert loose_title("1989 [nightcore]") == "1989"

    def test_still_drops_packaging(self):
        assert loose_title("Hysteria (Deluxe)") == "hysteria"


class TestNormArtist:
    def test_leading_article_kept(self):
        # Article handling is opt-in via without_article; artist names may
        # legitimately start with "The".
        assert norm_artist("The Cure") == "the cure"

    def test_ampersand_variant_equates(self):
        assert norm_artist("Simon & Garfunkel") == norm_artist("Simon and Garfunkel")


class TestWithoutArticle:
    def test_the_dropped(self):
        assert without_article("The Joshua Tree") == "joshua tree"

    def test_no_article_untouched(self):
        assert without_article("Joshua Tree") == "joshua tree"


class TestStripPackaging:
    def test_disc_kept_mid_string(self):
        # Only trailing splits are removed; a real subtitle survives.
        assert strip_packaging("Disc 1 of the Best") == "Disc 1 of the Best"


class TestAlbumKeys:
    def test_album_key_uses_norm(self):
        assert album_key("U2", "The Joshua Tree (Deluxe)") == "u2|the joshua tree"

    def test_loose_key_differs_on_bracket(self):
        assert loose_album_key("Taylor Swift", "1989 [nightcore]") == "taylor swift|1989"
        assert album_key("Taylor Swift", "1989 [nightcore]") == "taylor swift|1989 nightcore"


class TestCjk:
    def test_hangul_detected(self):
        assert has_cjk("싸다9")

    def test_kana_detected(self):
        assert has_cjk("浜崎あゆみ")

    def test_latin_not_detected(self):
        assert not has_cjk("Daft Punk")

    def test_punctuation_stripped(self):
        assert strip_punctuation("화양연화 pt.1") == "화양연화 pt 1"
