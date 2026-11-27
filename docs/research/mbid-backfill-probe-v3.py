"""Probe v3 — the two numbers the design actually needs.

1. Oracle validation: does Lidarr's foreignAlbumId really point at the release
   group whose title/artist matches the Navidrome album? (Sample, MB-checked.)
2. Remaining-gap feasibility: for the albums the oracle cannot cover, how many
   does artist-browse resolve, and does track-count verification agree?

Uses the real date fields (`maxYear` / `date`), not `year` — Navidrome's album
object has no `year` key at all, which is why v1's year gate never fired.
"""
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
OUT = "/tmp/mbid_v3.json"
ARTIST_SAMPLE = 20
ORACLE_SAMPLE = 20

ED = re.compile(r"deluxe|remaster|expanded|anniversary|bonus|special|reissue|collector|"
                r"limited|explicit|clean|mono|stereo|edition|version|vinyl|press|import", re.I)
JUNK_SEC = {"remix", "live", "demo", "bootleg", "interview", "mixtape/street", "dj-mix",
            "audiobook", "spokenword", "field recording"}
UNKNOWN = re.compile(r"^\[?\s*(unknown album|unknown|untitled|no album)\s*\]?$", re.I)


class MB:
    def __init__(self, gap=1.6):
        self.gap, self.last, self.calls, self.retries = gap, 0.0, 0, 0

    def get(self, path, **params):
        params["fmt"] = "json"
        url = f"https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}"
        for attempt, pause in enumerate((0, 5, 11, 20)):
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
                return {"_error": e.code}
            except urllib.error.URLError:
                if attempt < 3:
                    continue
                return {"_error": "network"}
        return {"_error": "exhausted"}


mb = MB()


def deacc(t):
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def norm(t):
    t = deacc(t).lower().replace("&", " and ")

    def br(m):
        return " " if ED.search(m.group(1)) else f" {m.group(1)} "

    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", br, t)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t)
    return " ".join(t.split())


def loose(t):
    t = deacc(t).lower().replace("&", " and ")
    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", " ", t)
    t = re.sub(r"[^0-9a-z]+", " ", t)
    return " ".join(t.split())


def na(t):
    t = deacc(t).lower().replace("&", " and ")
    return " ".join(re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t).split())


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

oracle, unmapped = [], []
for a in target:
    k = f"{na(a.get('albumArtist') or '')}|{norm(a.get('name') or '')}"
    lk = f"{na(a.get('albumArtist') or '')}|{loose(a.get('name') or '')}"
    if k in exact and len(exact[k]) == 1:
        oracle.append((a, next(iter(exact[k]))))
    elif lk in lmap and len(lmap[lk]) == 1:
        oracle.append((a, next(iter(lmap[lk]))))
    elif k not in exact and lk not in lmap:
        unmapped.append(a)

print(f"untagged {len(untagged)}, target {len(target)}, oracle {len(oracle)}, unmapped {len(unmapped)}")

# ── 1. oracle validation ────────────────────────────────────────────────────
random.seed(3)
sample = random.sample(oracle, min(ORACLE_SAMPLE, len(oracle)))
agree = part = disagree = 0
bad = []
for a, mbid in sample:
    rg = mb.get(f"release-group/{mbid}", inc="artist-credits")
    if rg.get("_error"):
        continue
    t_ok = norm(rg.get("title") or "") == norm(a.get("name") or "")
    l_ok = loose(rg.get("title") or "") == loose(a.get("name") or "")
    ar = na(" ".join(c.get("name") or "" for c in rg.get("artist-credit") or []))
    a_ok = ar == na(a.get("albumArtist") or "")
    ry = int((rg.get("first-release-date") or "0")[:4] or 0)
    y_ok = (yr(a) is None or not ry or abs(yr(a) - ry) <= 2)
    if t_ok and a_ok:
        agree += 1
    elif (t_ok or l_ok) and a_ok:
        part += 1
    else:
        disagree += 1
        bad.append((a.get("albumArtist"), a.get("name"), rg.get("title"),
                    " ".join(c.get("name") or "" for c in rg.get("artist-credit") or []),
                    t_ok, a_ok, f"year {yr(a)} vs {ry}"))
print(f"\n[1] oracle validation on {len(sample)}: exact agree {agree}, edition-only {part}, disagree {disagree}")
for b in bad[:6]:
    print(f"    {b}")

# ── 2. remaining gap via artist browse ──────────────────────────────────────
by_artist = {}
for a in unmapped:
    by_artist.setdefault(na(a.get("albumArtist") or ""), []).append(a)
pool = [k for k in by_artist if k]
random.shuffle(pool)
artists = pool[:ARTIST_SAMPLE]

tiers = {"high": 0, "medium": 0, "low": 0}
tracks_ok = tracks_bad = tracks_unknown = 0
unresolved_artist = 0
examined = 0
examples, misses = [], []

for key in artists:
    group = by_artist[key]
    display = group[0].get("albumArtist") or ""
    print(f"  artist {display[:40]!r} ({len(group)} albums)", flush=True)
    found = mb.get("artist", query=f'artist:"{display}"', limit=5)
    cands = found.get("artists", []) if not found.get("_error") else []
    hit = next((x for x in cands if na(x.get("name")) == key), None)
    if not hit:
        unresolved_artist += len(group)
        misses.append((display, group[0].get("name"), "artist unresolved"))
        continue
    rgs = []
    for t in ("album", "ep"):
        b = mb.get("release-group", artist=hit["id"], type=t, limit=100)
        if not b.get("_error"):
            rgs += b.get("release-groups", [])

    for a in group:
        examined += 1
        best, best_tier = None, None
        for c in rgs:
            sec = {s.lower() for s in c.get("secondary-types") or []}
            if sec & JUNK_SEC and not re.search(r"live|remix|demo", norm(a.get("name") or "")):
                continue
            ct, ca = norm(c.get("title") or ""), na(" ".join(x.get("name") or ""
                                                            for x in c.get("artist-credit") or []))
            if ca != key:
                continue
            if ct == norm(a.get("name") or ""):
                best, best_tier = c, "high"
                break
            if loose(c.get("title") or "") == loose(a.get("name") or "") and best_tier != "high":
                best, best_tier = c, "medium"
        if not best:
            misses.append((display, a.get("name"), "no title match in discography"))
            continue
        # year gate
        ry = int((best.get("first-release-date") or "0")[:4] or 0)
        if yr(a) and ry and abs(yr(a) - ry) > 2:
            best_tier = "low"
        # track-count gate
        detail = mb.get(f"release-group/{best['id']}", inc="releases+media")
        counts = set()
        for rel in detail.get("releases") or []:
            tc = rel.get("track-count") or sum(m.get("track-count") or 0
                                               for m in rel.get("media") or [])
            if tc:
                counts.add(tc)
        if not counts:
            tracks_unknown += 1
        elif any(abs(c - (a.get("songCount") or 0)) <= 1 for c in counts):
            tracks_ok += 1
        else:
            tracks_bad += 1
            best_tier = "low"
        tiers[best_tier] = tiers.get(best_tier, 0) + 1
        if len(examples) < 14:
            examples.append((f"{display} — {a.get('name')} [{yr(a)}, {a.get('songCount')} tr]",
                             f"{best.get('title')} ({(best.get('first-release-date') or '?')[:4]})",
                             best_tier, sorted(counts)[:4]))

print(f"\n[2] remaining gap: examined {examined} albums from {len(artists)} artists")
print(f"    high {tiers['high']}, medium {tiers['medium']}, low {tiers['low']}, "
      f"no match {examined - sum(tiers.values())}, artist unresolved {unresolved_artist}")
print(f"    track-count check: agree {tracks_ok}, disagree {tracks_bad}, unavailable {tracks_unknown}")
for local, match, tier, counts in examples:
    print(f"    [{tier}] {local} -> {match} mb_tracks={counts}")
print("    -- misses --")
for m in misses[:8]:
    print(f"      {m}")

json.dump({"untagged": len(untagged), "target": len(target), "oracle": len(oracle),
           "unmapped": len(unmapped),
           "oracle_validation": {"sample": len(sample), "agree": agree, "edition": part,
                                 "disagree": disagree, "bad": bad},
           "gap": {"examined": examined, "tiers": tiers, "tracks_ok": tracks_ok,
                   "tracks_bad": tracks_bad, "tracks_unknown": tracks_unknown,
                   "unresolved_artist": unresolved_artist, "examples": examples},
           "mb_calls": mb.calls, "retries": mb.retries},
          open(OUT, "w"), indent=1, ensure_ascii=False)
print(f"\nmb calls {mb.calls}, retries {mb.retries} -> {OUT}")
