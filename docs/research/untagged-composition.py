"""Composition of the untagged slice — what kind of album is failing to match?

No MusicBrainz calls; Navidrome only.
"""
import json
import re
import sqlite3
import unicodedata
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
DB = "/app/backend/data/localsync.db"

con = sqlite3.connect(DB)
nd_url, nd_user, nd_pass = con.execute(
    "SELECT navidrome_url, username, navidrome_password FROM users ORDER BY id LIMIT 1").fetchone()


def http_json(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


token = http_json(nd_url.rstrip("/") + "/auth/login",
                  headers={"Content-Type": "application/json"},
                  data=json.dumps({"username": nd_user, "password": nd_pass}).encode())["token"]
hd = {"x-nd-authorization": f"Bearer {token}"}
albums, start = [], 0
while True:
    page = http_json(f"{nd_url.rstrip('/')}/api/album?_start={start}&_end={start + 200}&_sort=name",
                     headers=hd)
    if not page:
        break
    albums += page
    start += 200

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
LATIN = re.compile(r"[A-Za-z]")
SYMBOL = re.compile(r"[^\w\s\-'&.,:!?/()\[\]]", re.UNICODE)
EDITION = re.compile(r"\b(deluxe|remaster|expanded|anniversary|bonus|special edition|"
                     r"reissue|collector|limited|explicit|clean|mono|stereo|edition)\b", re.I)
LIVEISH = re.compile(r"\b(live|vevo|remix|karaoke|tribute|instrumental|acoustic|"
                     r"session|demo|bootleg|mix(es)?)\b", re.I)
DISC = re.compile(r"\b(disc|cd)\s*\d+\b|\(\s*disc\s*\d+\s*\)", re.I)


def fold(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", t.lower()).split())


untagged = [a for a in albums if not a.get("mbzReleaseGroupId")]
tagged = [a for a in albums if a.get("mbzReleaseGroupId")]

groups = {"mojibake": [], "various_artists": [], "live_or_remix": [], "disc_subset": [],
          "edition_suffix": [], "non_latin": [], "plain": []}
for a in untagged:
    name = a.get("name") or ""
    artist = a.get("albumArtist") or a.get("artist") or ""
    if "\ufffd" in name or SYMBOL.search(name) or SYMBOL.search(artist):
        groups["mojibake"].append(a)
    elif fold(artist) in ("various artists", "various", "va"):
        groups["various_artists"].append(a)
    elif LIVEISH.search(name):
        groups["live_or_remix"].append(a)
    elif DISC.search(name):
        groups["disc_subset"].append(a)
    elif EDITION.search(name):
        groups["edition_suffix"].append(a)
    elif CJK.search(name):
        groups["non_latin"].append(a)
    else:
        groups["plain"].append(a)

print(f"albums {len(albums)}  tagged {len(tagged)}  untagged {len(untagged)}")
print("\nuntagged composition (first matching bucket wins):")
for k, v in groups.items():
    print(f"  {k:16s} {len(v):5d}  {len(v) / len(untagged) * 100:5.1f}%")

year_buckets = {"no year": 0, "year < 1990": 0, "1990-2009": 0, "2010+": 0}
for a in untagged:
    y = a.get("year")
    if not y:
        year_buckets["no year"] += 1
    elif y < 1990:
        year_buckets["year < 1990"] += 1
    elif y < 2010:
        year_buckets["1990-2009"] += 1
    else:
        year_buckets["2010+"] += 1
print("\nuntagged by year:")
for k, v in year_buckets.items():
    print(f"  {k:14s} {v:5d}  {v / len(untagged) * 100:5.1f}%")

one_track = sum(1 for a in untagged if (a.get("songCount") or 0) <= 1)
print(f"\nuntagged with <=1 track: {one_track} ({one_track / len(untagged) * 100:.1f}%) "
      f"— singles, MB often has no matching release group")
print(f"untagged by Various Artists: {len(groups['various_artists'])}")

print("\n-- mojibake examples --")
for a in groups["mojibake"][:10]:
    print(f"  {(a.get('albumArtist') or '?')!r} — {(a.get('name') or '')!r}")
print("\n-- plain examples (should be the easy wins) --")
for a in groups["plain"][:8]:
    print(f"  {(a.get('albumArtist') or '?')} — {a.get('name')} ({a.get('year')}, {a.get('songCount')} tr)")

json.dump({"albums": len(albums), "tagged": len(tagged), "untagged": len(untagged),
           "buckets": {k: len(v) for k, v in groups.items()},
           "years": year_buckets, "one_track": one_track},
          open("/tmp/untagged-composition.json", "w"), indent=1)
