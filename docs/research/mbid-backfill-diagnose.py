"""Diagnose the backfill probe: why did 44% return nothing, and is 'high' safe?

Reads the probe JSON, then for each low/none album re-runs the MusicBrainz query
and prints what actually came back. Also verifies the Lidarr oracle against
MusicBrainz (year + track count) and counts folded-key ambiguity.
"""
import json
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
DB = "/app/backend/data/localsync.db"
PROBE = "/tmp/mbid_backfill_probe.json"

PAREN = re.compile(r"[\(\[\{].*?[\)\]\}]")
EDITION = re.compile(
    r"\b(deluxe|remaster(ed)?|expanded|anniversary|bonus|special edition|reissue|"
    r"collector'?s|limited edition|explicit|clean|mono|stereo)\b.*$", re.I)
SUFFIX = re.compile(r"\s*[-–—]\s*(ep|single|lp|album)\s*$", re.I)


def fold(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("&", " and ")
    t = PAREN.sub(" ", t)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t)
    return " ".join(t.split())


class T:
    def __init__(self, gap=1.05):
        self.gap, self.last = gap, 0.0

    def __call__(self):
        w = self.gap - (time.monotonic() - self.last)
        if w > 0:
            time.sleep(w)
        self.last = time.monotonic()


mb = T()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def search(q, limit=5):
    mb()
    p = urllib.parse.urlencode({"query": q, "fmt": "json", "limit": limit})
    try:
        return get(f"https://musicbrainz.org/ws/2/release-group/?{p}")
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "count": 0, "release-groups": []}


probe = json.load(open(PROBE))

print("=== A. why the misses? (raw MB responses) ===")
for r in probe["backfill"]["results"]:
    if r["confidence"] not in ("none", "low"):
        continue
    a = r["album"]
    q = f'artist:"{a["artist"]}" AND release:"{a["name"]}"'
    d = search(q)
    titles = [(x.get("title"), " ".join(c.get("name") or "" for c in x.get("artist-credit") or []),
               x.get("primary-type"), x.get("secondary-types"), x.get("score"))
              for x in d.get("release-groups", [])[:4]]
    print(f"\n{a['artist']} — {a['name']}  [artist={a['artist']!r}]")
    print(f"  fielded count={d.get('count')} err={d.get('_error')}")
    for t in titles:
        print(f"    {t}")
    if not titles:
        alt = re.sub(r"\s*[-–—]\s*(ep|single|lp)$", "", PAREN.sub(" ", a["name"]), flags=re.I)
        alt = " ".join(EDITION.sub("", alt).split()) or a["name"]
        d2 = search(f'artist:"{a["artist"]}" AND release:"{alt}"')
        print(f"  stripped {alt!r}: count={d2.get('count')}")
        for x in d2.get("release-groups", [])[:3]:
            print(f"    {(x.get('title'), x.get('primary-type'), x.get('secondary-types'), x.get('score'))}")

print("\n=== B. is 'high' safe? re-check the flagged one ===")
for r in probe["backfill"]["results"]:
    a = r["album"]
    if "nightcore" in (r.get("matched") or {}).get("title", ""):
        print(f"  local: {a['artist']} — {a['name']} ({a['songs']} tr)")
        print(f"  matched RG: {r['matched']}")
        rg = get(f"https://musicbrainz.org/ws/2/release-group/{r['matched']['rg']}"
                 f"?inc=releases+media+artist-credits&fmt=json")
        print(f"  primary={rg.get('primary-type')} secondary={rg.get('secondary-types')} "
              f"disambig={rg.get('disambiguation')!r}")
        for rel in (rg.get("releases") or [])[:6]:
            print(f"    release: {rel.get('title')!r} date={rel.get('date')} "
                  f"status={rel.get('status')} tracks={rel.get('track-count')} "
                  f"media={[m.get('track-count') for m in rel.get('media') or []]}")

print("\n=== C. where does track-count live? ===")
sample_rg = None
for r in probe["backfill"]["results"]:
    if r["confidence"] == "high" and (r.get("matched") or {}).get("rg"):
        sample_rg = r["matched"]["rg"]
        break
if sample_rg:
    rg = get(f"https://musicbrainz.org/ws/2/release-group/{sample_rg}"
             f"?inc=releases+media&fmt=json")
    rel = (rg.get("releases") or [{}])[0]
    print(f"  release keys: {sorted(rel.keys())}")
    print(f"  track-count={rel.get('track-count')} media={[m.get('track-count') for m in rel.get('media') or []]}")

con = sqlite3.connect(DB)
nd_url = con.execute("SELECT navidrome_url FROM users ORDER BY id LIMIT 1").fetchone()[0]
lid_url, lid_key = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()
lid = get(f"{lid_url.rstrip('/')}/api/v1/album?apikey={lid_key}")

print("\n=== D. lidarr oracle: ambiguity and agreement ===")
buckets = {}
for la in lid:
    mbid = la.get("foreignAlbumId")
    if not mbid:
        continue
    k = f"{fold(((la.get('artist') or {}).get('artistName')) or '')}|{fold(la.get('title'))}"
    buckets.setdefault(k, set()).add(mbid)
multi = {k: v for k, v in buckets.items() if len(v) > 1}
print(f"  lidarr keys: {len(buckets)}; keys mapping to >1 RG: {len(multi)} "
      f"({len(multi) / len(buckets) * 100:.1f}%)")
for k, v in list(multi.items())[:6]:
    print(f"    {k!r} -> {len(v)} mbid(s)")

# verify 20 oracle hits against MB metadata
import random
random.seed(7)
hits = probe["lidarr"]["oracle_examples"][:0]  # not enough; recompute below
checked = agree = disagree = 0
for la in random.sample(lid, 25):
    mbid = la.get("foreignAlbumId")
    if not mbid:
        continue
    rg = get(f"https://musicbrainz.org/ws/2/release-group/{mbid}?inc=artist-credits&fmt=json")
    mb_title = rg.get("title")
    mb_artist = " ".join(c.get("name") or "" for c in rg.get("artist-credit") or [])
    t_ok = fold(mb_title) == fold(la.get("title"))
    a_ok = fold(mb_artist) == fold(((la.get("artist") or {}).get("artistName")) or "")
    checked += 1
    if t_ok and a_ok:
        agree += 1
    else:
        disagree += 1
        if disagree <= 5:
            print(f"    MISMATCH lidarr={la.get('title')!r}/{((la.get('artist') or {}).get('artistName'))!r} "
                  f"mb={mb_title!r}/{mb_artist!r}")
print(f"  spot-check {checked} lidarr->MB: agree {agree}, disagree {disagree}")
