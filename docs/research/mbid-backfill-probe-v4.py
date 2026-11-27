"""Probe v4 — fill rate for the tail the Lidarr oracle cannot reach (300 albums).

v3 died from MusicBrainz 503s (35 retries in 101 calls), so its "no match" rows
measured throttling, not matching. This run is deliberately small and slow, and
fixes three normalisation gaps seen in v3's misses:

  * "(CD 1)" / "(Disc 2)" disc-split suffixes must always be stripped.
  * Artist name order swaps ("Utada Hikaru" vs MB's "Hikaru Utada") need an
    unfielded artist-search fallback.
  * Misspelled artist tags ("Ozzy Osboune") need fuzzy acceptance, gated by
    title + track-count agreement.
"""
import difflib
import json
import random
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
DB = "/app/backend/data/localsync.db"
OUT = "/tmp/mbid_v4.json"
ARTIST_SAMPLE = 12
GAP = 2.5

ED = re.compile(r"deluxe|remaster|expanded|anniversary|bonus|special|reissue|collector|"
                r"limited|explicit|clean|mono|stereo|edition|version|vinyl|press|import|"
                r"japan|karaoke|instrumental", re.I)
DISC = re.compile(r"\s*[\(\[]\s*(cd|disc|disk)\s*\d+\s*[\)\]]\s*$", re.I)
JUNK_SEC = {"remix", "demo", "bootleg", "interview", "mixtape/street", "dj-mix",
            "audiobook", "spokenword", "field recording"}
UNKNOWN = re.compile(r"^\[?\s*(unknown album|unknown|untitled|no album)\s*\]?$", re.I)
CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"


class MB:
    def __init__(self, gap=GAP):
        self.gap, self.last, self.calls, self.retries, self.failed = gap, 0.0, 0, 0, 0

    def get(self, path, **params):
        params["fmt"] = "json"
        url = f"https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}"
        for attempt, pause in enumerate((0, 6, 14, 30)):
            if pause:
                time.sleep(pause)
                self.retries += 1
            w = self.gap - (time.monotonic() - self.last)
            if w > 0:
                time.sleep(w)
            self.last = time.monotonic()
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=45) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < 3:
                    continue
                self.failed += 1
                return {"_error": e.code}
            except urllib.error.URLError:
                if attempt < 3:
                    continue
                self.failed += 1
                return {"_error": "network"}
        self.failed += 1
        return {"_error": "exhausted"}


mb = MB()


def deacc(t):
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def clean(t):
    """Drop disc-split suffixes and an optional trailing '- EP' style marker."""
    t = DISC.sub("", t or "")
    t = re.sub(r"\s*[-–—]\s*(ep|single|lp|album)\s*$", "", t, flags=re.I)
    return " ".join(t.split())


def norm(t):
    t = deacc(clean(t)).lower().replace("&", " and ")

    def br(m):
        return " " if ED.search(m.group(1)) else f" {m.group(1)} "

    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", br, t)
    t = re.sub(rf"[^0-9a-z{CJK}]+", " ", t)
    return " ".join(t.split())


def loose(t):
    t = deacc(clean(t)).lower().replace("&", " and ")
    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", " ", t)
    return " ".join(re.sub(rf"[^0-9a-z{CJK}]+", " ", t).split())


def na(t):
    t = deacc(t).lower().replace("&", " and ")
    return " ".join(re.sub(rf"[^0-9a-z{CJK}]+", " ", t).split())


def http_json(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


con = sqlite3.connect(DB)
nd_url, nd_user, nd_pass = con.execute(
    "SELECT navidrome_url, username, navidrome_password FROM users ORDER BY id LIMIT 1").fetchone()
lid_url, lid_key = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()

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


def yr(a):
    if a.get("maxYear"):
        return a["maxYear"]
    d = (a.get("date") or "")[:4]
    return int(d) if d.isdigit() else None


def garbled(t):
    if "\ufffd" in t:
        return True
    nl = sum(1 for c in t if ord(c) > 0x2E80)
    la = sum(1 for c in t if c.isascii() and c.isalpha())
    return bool(t) and la and 0 < nl < 3


untagged = [a for a in albums if not a.get("mbzReleaseGroupId")]
target = [a for a in untagged if (a.get("songCount") or 0) > 1
          and not UNKNOWN.match((a.get("name") or "").strip())
          and not garbled(a.get("name") or "")]

lid = http_json(f"{lid_url.rstrip('/')}/api/v1/album?apikey={lid_key}")
exact, lmap = {}, {}
for la in lid:
    m = la.get("foreignAlbumId")
    if not m:
        continue
    ar = (la.get("artist") or {}).get("artistName") or ""
    exact.setdefault(f"{na(ar)}|{norm(la.get('title') or '')}", set()).add(m)
    lmap.setdefault(f"{na(ar)}|{loose(la.get('title') or '')}", set()).add(m)

unmapped = [a for a in target
            if not (f"{na(a.get('albumArtist') or '')}|{norm(a.get('name') or '')}" in exact
                    and len(exact[f"{na(a.get('albumArtist') or '')}|{norm(a.get('name') or '')}"]) == 1)
            and not (f"{na(a.get('albumArtist') or '')}|{loose(a.get('name') or '')}" in lmap
                     and len(lmap[f"{na(a.get('albumArtist') or '')}|{loose(a.get('name') or '')}"]) == 1)]
print(f"target {len(target)}, unmapped {len(unmapped)}", flush=True)

by_artist = {}
for a in unmapped:
    by_artist.setdefault(na(a.get("albumArtist") or ""), []).append(a)
random.seed(5)
pool = [k for k in by_artist if k]
random.shuffle(pool)
artists = pool[:ARTIST_SAMPLE]

outcomes = []
for key in artists:
    group = by_artist[key]
    display = group[0].get("albumArtist") or ""

    cands = []
    for q in (f'artist:"{display}"', display):
        res = mb.get("artist", query=q, limit=5)
        if res.get("_error"):
            continue
        cands = res.get("artists", [])
        if cands:
            break

    best_artist, best_ratio = None, 0.0
    for c in cands:
        n = na(c.get("name"))
        ratio = 1.0 if n == key else difflib.SequenceMatcher(None, n, key).ratio()
        if ratio > best_ratio:
            best_artist, best_ratio = c, ratio
    if not best_artist or best_ratio < 0.85:
        for a in group:
            outcomes.append((display, a.get("name"), "artist unresolved",
                             f"best={best_artist.get('name') if best_artist else None} r={best_ratio:.2f}"))
        print(f"  {display[:38]!r}: artist unresolved (r={best_ratio:.2f})", flush=True)
        continue

    rgs = []
    for t in ("album", "ep"):
        b = mb.get("release-group", artist=best_artist["id"], type=t, limit=100)
        if not b.get("_error"):
            rgs += b.get("release-groups", [])

    for a in group:
        local_t, local_lo = norm(a.get("name") or ""), loose(a.get("name") or "")
        cands2 = []
        for c in rgs:
            sec = {s.lower() for s in c.get("secondary-types") or []}
            if sec & JUNK_SEC and not re.search(r"live|remix|demo", local_t):
                continue
            if (c.get("primary-type") or "Album").lower() not in ("album", "ep"):
                continue
            ct = norm(c.get("title") or "")
            if ct == local_t:
                cands2.append((0, c, "exact"))
            elif loose(c.get("title") or "") == local_lo:
                cands2.append((1, c, "edition"))
        if not cands2:
            outcomes.append((display, a.get("name"), "no title match", ""))
            print(f"  {display[:30]!r} — {(a.get('name') or '')[:40]!r}: no title match", flush=True)
            continue
        cands2.sort(key=lambda x: x[0])
        tier, cand, how = cands2[0]

        ry = int((cand.get("first-release-date") or "0")[:4] or 0)
        y_ok = not (yr(a) and ry and abs(yr(a) - ry) > 2)
        detail = mb.get(f"release-group/{cand['id']}", inc="releases+media")
        counts = set()
        for rel in detail.get("releases") or []:
            tc = rel.get("track-count") or sum(m.get("track-count") or 0
                                               for m in rel.get("media") or [])
            if tc:
                counts.add(tc)
        t_ok = any(abs(c - (a.get("songCount") or 0)) <= 1 for c in counts) if counts else None

        if tier == 0 and y_ok and t_ok is True:
            conf = "high"
        elif tier == 0 and (t_ok is None or y_ok):
            conf = "medium"
        elif tier == 1 and y_ok and t_ok is True:
            conf = "medium"
        else:
            conf = "low"
        outcomes.append((display, a.get("name"), conf,
                         f"{cand.get('title')} | {how} | yr {yr(a)}/{ry} | "
                         f"tracks {a.get('songCount')} vs {sorted(counts)[:3]}"))
        print(f"  [{conf}] {display[:26]!r} — {(a.get('name') or '')[:34]!r} -> "
              f"{cand.get('title')!r} ({how}, tracks {sorted(counts)[:3]})", flush=True)

tally = {}
for o in outcomes:
    tally[o[2]] = tally.get(o[2], 0) + 1
print(f"\ntail outcomes on {len(outcomes)} albums from {len(artists)} artists: {tally}")
print(f"mb calls {mb.calls}, retries {mb.retries}, failed {mb.failed}")
json.dump({"outcomes": outcomes, "tally": tally, "calls": mb.calls,
           "retries": mb.retries, "failed": mb.failed, "unmapped": len(unmapped),
           "target": len(target), "artists_sampled": len(artists)},
          open(OUT, "w"), indent=1, ensure_ascii=False)
print(f"-> {OUT}")
