"""Title and artist normalisation, shared by catalog search and MBID backfill.

Phase 0 measured why this module exists. Matching an owned album to a MusicBrainz
release group fails on text, not on data:

* ``U2 — Joshua Tree`` vs ``The Joshua Tree`` (leading article)
* ``Usher — My Way: 25th Anniversary Edition`` vs ``My Way`` (edition phrase
  outside brackets)
* ``Smashing Pumpkins — ATUM ... - Act 1`` vs one ``ATUM`` release group
  (a disc/act split has been flattened into a fake album)
* ``Taylor Swift — 1989 [nightcore]`` folding to ``1989`` and being accepted as
  the real album (an over-eager bracket strip)

So there are deliberately two title keys. ``norm_title`` strips only brackets that
look like packaging (deluxe, remaster, anniversary…), which keeps
``1989 [nightcore]`` distinct from ``1989``. ``loose_title`` strips every bracket
and is a *weaker* tier — it may confirm a candidate but must never be the sole
reason to accept one.

Everything here is pure and synchronous so it can be unit-tested and used from
both async clients and the offline backfill job.
"""
import re
import unicodedata

# CJK ideographs, kana and hangul. Used to pick a field-scoped query shape:
# `releasegroup:"화양연화 pt.1"` returns 503, while `releasegroup:(화양연화)` works.
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

# Bracketed text that is packaging, not identity.
_EDITION_TOKENS = re.compile(
    r"deluxe|remaster|expanded|anniversary|bonus|special|reissue|collector|"
    r"limited|explicit|clean|mono|stereo|edition|version|vinyl|press|import|"
    r"japan|karaoke|instrumental|promo|digipak|remastered",
    re.I,
)

# Trailing disc/act splits that a ripper turned into a separate "album".
_DISC_SUFFIX = re.compile(
    r"\s*[\(\[]\s*(cd|disc|disk)\s*\d+\s*[\)\]]\s*$|"
    r"\s*[-–—]\s*(act|part|pt\.?)\s*\d+\s*$",
    re.I,
)

# Trailing "- EP" / " - Single" markers; the primary type carries this on MB.
_TYPE_SUFFIX = re.compile(r"\s*[-–—]\s*(ep|single|lp|album)\s*$", re.I)

_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)

_NON_WORD = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")


def deaccent(text: str) -> str:
    """Strip combining marks so ``Röyksopp`` and ``Royksopp`` compare equal.

    Recomposed with NFC afterwards: NFKD splits Hangul syllables into jamo,
    which are outside the CJK ranges the rest of this module preserves, so
    ``싸다9`` would otherwise be reduced to ``9``.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped)


def strip_packaging(text: str) -> str:
    """Drop disc/act splits and a trailing ``- EP`` style marker."""
    text = _DISC_SUFFIX.sub("", text or "")
    text = _TYPE_SUFFIX.sub("", text)
    return " ".join(text.split())


def _bracket_sub(replacement: str):
    def sub(match: re.Match) -> str:
        inner = match.group(1)
        if replacement == "drop_if_packaging":
            return " " if _EDITION_TOKENS.search(inner) else f" {inner} "
        return " "

    return sub


def _squash(text: str) -> str:
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def fold(text: str) -> str:
    """Case/diacritic/punctuation-insensitive key that keeps CJK characters.

    This is the comparison primitive — it does not try to understand packaging.
    """
    text = deaccent(text).lower().replace("&", " and ")
    return _squash(text)


def norm_title(text: str) -> str:
    """Edition-aware title key.

    Brackets holding packaging (``(Deluxe Edition)``, ``[Remastered]``) are
    removed; brackets holding anything else keep their words, so
    ``1989 [nightcore]`` stays distinguishable from ``1989``.
    """
    text = deaccent(strip_packaging(text)).lower().replace("&", " and ")
    text = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", _bracket_sub("drop_if_packaging"), text)
    return _squash(text)


def loose_title(text: str) -> str:
    """Every bracket dropped. A weaker tier: confirm only, never accept alone."""
    text = deaccent(strip_packaging(text)).lower().replace("&", " and ")
    text = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", " ", text)
    return _squash(text)


def norm_artist(text: str) -> str:
    return _squash(deaccent(text or "").lower().replace("&", " and "))


def without_article(text: str) -> str:
    """``The Joshua Tree`` -> ``joshua tree`` for a last-resort title comparison."""
    return _LEADING_ARTICLE.sub("", norm_title(text))


def has_cjk(text: str) -> bool:
    return bool(CJK.search(text or ""))


def strip_punctuation(text: str) -> str:
    """Remove punctuation but keep word characters, including CJK.

    Used when building a field-scoped query for a non-Latin title: MB's query
    parser answers 503 to punctuation inside a quoted field.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", text or "", flags=re.UNICODE).split())


def album_key(artist: str, album: str) -> str:
    """Folded ``artist|album`` key used for name-tier ownership and oracle joins."""
    return f"{norm_artist(artist)}|{norm_title(album)}"


def loose_album_key(artist: str, album: str) -> str:
    return f"{norm_artist(artist)}|{loose_title(album)}"
