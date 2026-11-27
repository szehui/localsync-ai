"""Read-only probe: how far can we close the release-group MBID gap?

Three candidate strategies, measured on the real library:

  A. Lidarr as an oracle — Lidarr album rows carry foreignAlbumId (the RG MBID)
     and were themselves created by MBID lookups. Fold-match artist|title from
     Navidrome to Lidarr and copy the id. Zero MusicBrainz calls.
  B. MusicBrainz search backfill — fielded artist+release query, then verify
     year / track count / type before trusting the top result.
  C. Release-MBID -> RG conversion — only helps albums that have mbzAlbumId but
     no mbzReleaseGroupId, so measure that slice first.

Nothing is written to Navidrome or Lidarr. Credentials are read from the app DB
and never printed.

Run inside the localsync-ai container so the DB path and service network match.
"""
import json
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
DB = "/app/backend/data/localsync.db"
OUT = "/tmp/mbid_backfill_probe.json"

SAMPLE_SIZE = 48          # untagged albums put through MusicBrainz
MB_DELAY = 1.05           # MB allows 1 req/s sustained

EDITION = re.compile(
    r"\b(deluxe|deluxe edition|remaster(ed)?|expanded|anniversary|bonus track(s)?|"
    r"special edition|reissue|collector'?s edition|limited edition|"
    r"explicit|clean|mono|stereo|version)\b.*$",
    re.I,
)
PAREN = re.compile(r"[\(\[\{].*?[\)\]\}]")


# ── helpers ──────────────────────────────────────────────────────────────────
def fold(text: str) -> str:
    """Case/diacritic/punctuation-insensitive key."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = PAREN.sub(" ", text)
    text = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", text)
    return " ".join(text.split())


def strip_edition(text: str) -> str:
    return " ".join(EDITION.sub("", PAREN.sub(" ", text or "")).split())


def title_matches(album_title: str, cand_title: str) -> bool:
    a, b = fold(album_title), fold(cand_title)
    if a == b:
        return True
    # "Album" vs "Album Deluxe" and vice versa
    sa, sb = fold(strip_edition(album_title)), fold(strip_edition(cand_title))
    return bool(sa) and sa == sb


def artist_matches(album_artist: str, credits: list) -> bool:
    """MB artist-credit rows -> flat name; tolerate 'A feat. B' and Various Artists."""
    if not album_artist:
        return False
    flat = fold(" ".join(c.get("name") or "" for c in credits or []))
    want = fold(album_artist)
    if not flat or not want:
        return False
    if want in ("various artists", "various", "va"):
        return flat in ("various artists", "various", "va") or len(credits or []) > 2
    if flat == want:
        return True
    # credit lists ("A & B") vs Navidrome's albumArtist ("A")
    if want in flat.split(" and ") or flat in want:
        return True
    first = fold((credits or [{}])[0].get("name") or "")
    return bool(first) and (first == want or want in first or first in want)


def http_json(url: str, headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


class Throttle:
    def __init__(self, gap=MB_DELAY):
        self.gap, self.last = gap, 0.0

    def __call__(self):
        wait = self.gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()


mb = Throttle()


def mb_search(query: str, limit=5) -> list:
    params = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": limit})
    mb()
    try:
        data = http_json(f"https://musicbrainz.org/ws/2/release-group/?{params}")
    except urllib.error.HTTPError as e:
        return [{"_error": e.code}]
    return data.get("release-groups", [])


def mb_rg_detail(mbid: str) -> dict:
    params = urllib.parse.urlencode({"inc": "releases+artist-credits", "fmt": "json"})
    mb()
    try:
        return http_json(f"https://musicbrainz.org/ws/2/release-group/{mbid}?{params}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code}


# ── data sources ────────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
nd_url, nd_user, nd_pass = con.execute(
    "SELECT navidrome_url, username, navidrome_password FROM users ORDER BY id LIMIT 1"
).fetchone()
lid_url, lid_key = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()

token = http_json(
    nd_url.rstrip("/") + "/auth/login",
    headers={"Content-Type": "application/json"},
    data=json.dumps({"username": nd_user, "password": nd_pass}).encode(),
)["token"]
nd_headers = {"x-nd-authorization": f"Bearer {token}"}


def nd_get(path: str):
    return http_json(f"{nd_url.rstrip('/')}/api/{path}", headers=nd_headers)


albums, start = [], 0
while True:
    page = nd_get(f"album?_start={start}&_end={start + 200}&_sort=name")
    if not page:
        break
    albums += page
    start += 200
print(f"navidrome albums: {len(albums)}")

rg = [a for a in albums if a.get("mbzReleaseGroupId")]
rel = [a for a in albums if a.get("mbzAlbumId")]
both = [a for a in albums if a.get("mbzReleaseGroupId") and a.get("mbzAlbumId")]
rel_only = [a for a in albums if a.get("mbzAlbumId") and not a.get("mbzReleaseGroupId")]
rg_only = [a for a in albums if a.get("mbzReleaseGroupId") and not a.get("mbzAlbumId")]
untagged = [a for a in albums if not a.get("mbzReleaseGroupId")]

print(f"  rg mbid:        {len(rg)}")
print(f"  release mbid:   {len(rel)}")
print(f"  both:           {len(both)}")
print(f"  release only:   {len(rel_only)}   <- strategy C ceiling")
print(f"  rg only:        {len(rg_only)}")
print(f"  untagged:       {len(untagged)}")

# ── strategy A: Lidarr as oracle ────────────────────────────────────────────
lid_albums = http_json(f"{lid_url.rstrip('/')}/api/v1/album?apikey={lid_key}")
print(f"lidarr albums: {len(lid_albums)}")

exact_map, loose_map = {}, {}
for la in lid_albums:
    mbid = la.get("foreignAlbumId")
    if not mbid:
        continue
    artist = ((la.get("artist") or {}).get("artistName")) or ""
    exact_map.setdefault(f"{fold(artist)}|{fold(la.get('title'))}", mbid)
    loose_map.setdefault(
        f"{fold(strip_edition(artist))}|{fold(strip_edition(la.get('title') or ''))}", mbid
    )

oracle_hits, oracle_loose = [], []
for a in untagged:
    artist = a.get("albumArtist") or a.get("artist") or ""
    key = f"{fold(artist)}|{fold(a.get('name'))}"
    loose = f"{fold(strip_edition(artist))}|{fold(strip_edition(a.get('name') or ''))}"
    if key in exact_map:
        oracle_hits.append((a, exact_map[key], "exact"))
    elif loose in loose_map:
        oracle_loose.append((a, loose_map[loose], "loose"))

print(f"\n[A] lidarr oracle on untagged: exact {len(oracle_hits)}, "
      f"edition-stripped {len(oracle_loose)} "
      f"({(len(oracle_hits) + len(oracle_loose)) / len(untagged) * 100:.1f}% of untagged)")

lidarr_keys = set(exact_map) | set(loose_map)
hit_mbids = {m for _, m, _ in oracle_hits} | {m for _, m, _ in oracle_loose}

# ── sampling for strategy B ─────────────────────────────────────────────────
random.seed(20261127)
non_ascii = [a for a in untagged if any(ord(c) > 0x2500 for c in a.get("name") or "")]
va = [a for a in untagged if fold(a.get("albumArtist") or a.get("artist") or "") in
      ("various artists", "various", "va")]
plain = [a for a in untagged if a not in non_ascii and a not in va]
random.shuffle(plain)
random.shuffle(non_ascii)
random.shuffle(va)
sample = plain[:SAMPLE_SIZE - 14] + non_ascii[:8] + va[:6]

print(f"\n[B] sampling {len(sample)} untagged albums "
      f"({len(plain[:SAMPLE_SIZE - 14])} plain, {len(non_ascii[:8])} non-ascii, {len(va[:6])} various-artists)")

results = []
for a in sample:
    artist = a.get("albumArtist") or a.get("artist") or ""
    title = a.get("name") or ""
    year = a.get("year")
    songs = a.get("songCount")

    cands = mb_search(f'artist:"{artist}" AND release:"{title}"', limit=5)
    query_used = "fielded"
    if not [c for c in cands if not c.get("_error")]:
        cands = mb_search(f"{artist} {title}", limit=5)
        query_used = "unfielded"

    plausible = []
    for c in cands:
        if c.get("_error"):
            continue
        c_artist = artist_matches(artist, c.get("artist-credit"))
        c_title = title_matches(title, c.get("title") or "")
        plausible.append({"rg": c.get("id"), "title": c.get("title"),
                          "artist": " ".join(x.get("name") or "" for x in c.get("artist-credit") or []),
                          "first_release_date": c.get("first-release-date"),
                          "primary": c.get("primary-type"),
                          "secondary": c.get("secondary-types") or [],
                          "score": c.get("score"),
                          "artist_ok": c_artist, "title_ok": c_title})

    strong = [p for p in plausible if p["artist_ok"] and p["title_ok"]]
    top = strong[0] if strong else (plausible[0] if plausible else None)

    detail, tracks, ydelta = None, None, None
    if top and top["artist_ok"] and top["title_ok"]:
        detail = mb_rg_detail(top["rg"])
        dates = [r.get("date") for r in detail.get("releases") or [] if r.get("date")]
        cand_year = int((top.get("first_release_date") or (dates[0] if dates else "") or "0")[:4] or 0)
        if year and cand_year:
            ydelta = abs(int(year) - cand_year)
        tc = [r.get("track-count") for r in detail.get("releases") or [] if r.get("track-count")]
        tracks = min(tc, key=lambda n: abs(n - (songs or n))) if tc else None

    if not top:
        confidence = "none"
    elif not (top["artist_ok"] and top["title_ok"]):
        confidence = "low"
    elif ydelta is not None and ydelta > 1:
        confidence = "medium"
    elif tracks is not None and songs and abs(tracks - songs) > 1:
        confidence = "medium"
    else:
        confidence = "high"

    results.append({
        "album": {"name": title, "artist": artist, "year": year, "songs": songs,
                  "non_ascii": a in non_ascii, "va": a in va},
        "query": query_used,
        "confidence": confidence,
        "ambiguous": len(strong) > 1,
        "matched": top and {k: top[k] for k in
                            ("rg", "title", "artist", "first_release_date", "primary", "secondary", "score")},
        "candidates": plausible[:3],
        "track_count_mb": tracks,
        "year_delta": ydelta,
        "artist_ok": bool(top and top["artist_ok"]),
        "title_ok": bool(top and top["title_ok"]),
    })

counts = {}
for r in results:
    counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1

print("\n[B] confidence distribution:")
for k in ("high", "medium", "low", "none"):
    n = counts.get(k, 0)
    print(f"  {k:6s} {n:3d}  ({n / len(results) * 100:5.1f}%)")
print(f"  ambiguous (2+ strong candidates): {sum(1 for r in results if r['ambiguous'])}")

tagged_now = len(rg)
oracle_gain = len(hit_mbids - {a.get("mbzReleaseGroupId") for a in albums if a.get("mbzReleaseGroupId")})
proj_high = counts.get("high", 0) / len(results) * (len(untagged) - len(hit_mbids))

print("\nprojection:")
print(f"  current MBID coverage:      {tagged_now}/{len(albums)} = {tagged_now / len(albums) * 100:.1f}%")
print(f"  + lidarr oracle:            {tagged_now + oracle_gain}/{len(albums)} = "
      f"{(tagged_now + oracle_gain) / len(albums) * 100:.1f}%")
print(f"  + high-confidence backfill: {tagged_now + oracle_gain + proj_high:.0f}/{len(albums)} = "
      f"{(tagged_now + oracle_gain + proj_high) / len(albums) * 100:.1f}%  (projected)")

payload = {
    "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "albums": len(albums), "rg": len(rg), "release_mbid": len(rel),
    "both": len(both), "release_only": len(rel_only), "rg_only": len(rg_only),
    "untagged": len(untagged),
    "lidarr": {"albums": len(lid_albums), "keys": len(lidarr_keys),
               "oracle_exact": len(oracle_hits), "oracle_loose": len(oracle_loose),
               "oracle_examples": [{"navidrome": f"{a.get('albumArtist')} — {a.get('name')}",
                                     "mbid": m, "how": h} for a, m, h in
                                    (oracle_hits + oracle_loose)[:12]]},
    "backfill": {"sample": len(results), "counts": counts,
                 "ambiguous": sum(1 for r in results if r["ambiguous"]),
                 "results": results},
}
with open(OUT, "w") as fh:
    json.dump(payload, fh, indent=1, ensure_ascii=False)

print(f"\nfull detail: {OUT}")
print("\n-- sample high-confidence matches --")
for r in [x for x in results if x["confidence"] == "high"][:8]:
    m = r["matched"]
    print(f"  {r['album']['artist']} — {r['album']['name']} ({r['album']['year']}, "
          f"{r['album']['songs']} tr)  ->  {m['artist']} — {m['title']} "
          f"({(m['first_release_date'] or '?')[:4]}, mb {r['track_count_mb']} tr, score {m['score']})")

print("\n-- sample low/none --")
for r in [x for x in results if x["confidence"] in ("low", "none")][:8]:
    m = r.get("matched") or {}
    print(f"  {r['album']['artist']} — {r['album']['name']} -> {m.get('title', '(nothing)')} "
          f"[artist_ok={r['artist_ok']} title_ok={r['title_ok']}]")
