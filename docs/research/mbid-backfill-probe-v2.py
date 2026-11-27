"""Probe v2: correct the two defects in v1 and test a better strategy.

v1 defects:
  1. No retry on HTTP 503. A large share of "no match" rows were MB throttling,
     not absent data. Fixed with 429/502/503/504 backoff.
  2. Title folding stripped *every* bracketed group, so "1989 [nightcore]"
     folded to "1989" and was accepted as Taylor Swift's 1989. Now only
     edition-style brackets are stripped; anything else keeps its text and
     scores as a looser match.

Strategies measured:
  A. Lidarr oracle — folded artist|title from Navidrome to Lidarr's library and
     copy foreignAlbumId. Ambiguity is counted (a folded key mapping to more
     than one RG is exactly the Greatest-Hits collision).
  B. Artist-browse — resolve the artist once, browse their album release groups,
     then score every untagged album for that artist locally. Amortises the API
     cost across a discography and sidesteps title-search fragility.
  C. Per-album fielded search, same albums, for comparison.
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
OUT = "/tmp/mbid_backfill_v2.json"

ARTIST_SAMPLE = 30
MB_GAP = 1.15

ED_TOKENS = re.compile(
    r"deluxe|remaster|expanded|anniversary|bonus|special|reissue|collector|limited|"
    r"explicit|clean|mono|stereo|edition|version|remix|live|vinyl|press|import|"
    r"catalog|cat no|japan|uk|us |disc \d|cd ?\d", re.I)
JUNK_SECONDARY = {"remix", "live", "demo", "bootleg", "interview", "mixtape/street",
                  "dj-mix", "audiobook", "spokenword", "field recording"}


def _deaccent(t):
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def norm_title(t):
    """Edition-aware title key. Unknown brackets keep their text."""
    t = _deaccent(t).lower().replace("&", " and ")

    def bracket(m):
        inner = m.group(1)
        return " " if ED_TOKENS.search(inner) else f" {inner} "

    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", bracket, t)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t)
    return " ".join(t.split())


def loose_title(t):
    """All brackets dropped — a looser tier, never auto-accepted."""
    t = _deaccent(t).lower().replace("&", " and ")
    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", " ", t)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t)
    return " ".join(t.split())


def norm_artist(t):
    return " ".join(re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+",
                           " ", _deaccent(t or "").lower().replace("&", " and ")).split())


class MB:
    def __init__(self, gap=MB_GAP):
        self.gap, self.last = gap, 0.0
        self.calls = 503 if False else 0
        self.retries = 0

    def _wait(self):
        w = self.gap - (time.monotonic() - self.last)
        if w > 0:
            time.sleep(w)
        self.last = time.monotonic()

    def get(self, path, **params):
        params["fmt"] = "json"
        url = f"https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}"
        for attempt, pause in enumerate((0, 4, 9, 18)):
            if pause:
                time.sleep(pause)
                self.retries += 1
            self._wait()
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=40) as r:
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


def credits_name(credits):
    return " ".join(c.get("name") or "" for c in credits or [])


def score_candidate(local, cand):
    """Return (tier, reasons) for one local album vs one MB release group."""
    lt, lta, lloose = norm_title(local["name"]), norm_artist(local["artist"]), loose_title(local["name"])
    ct, ca = norm_title(cand.get("title")), norm_artist(credits_name(cand.get("artist-credit")))
    reasons = []
    if lt and ct and lt == ct:
        title_tier = "exact"
    elif loose_title(cand.get("title")) == lloose and lloose:
        title_tier = "loose"
        reasons.append("title differs only by bracketed text")
    else:
        return None, None

    if ca == lta:
        artist_tier = "exact"
    elif lta and ca and (lta in ca or ca in lta) and min(len(lta), len(ca)) >= 5:
        artist_tier = "loose"
        reasons.append("artist name variant")
    else:
        return None, None

    secondary = {s.lower() for s in cand.get("secondary-types") or []}
    junk = secondary & JUNK_SECONDARY
    if junk and not any(t in lt for t in ("live", "remix", "demo")):
        return None, None
    if (cand.get("primary-type") or "Album").lower() not in ("album", "ep"):
        return None, None

    local_year = local.get("year")
    cand_year = int((cand.get("first-release-date") or "0")[:4] or 0)
    ydelta = abs(int(local_year) - cand_year) if local_year and cand_year else None
    if ydelta is not None and ydelta > 2:
        return None, None

    if title_tier == "exact" and artist_tier == "exact" and (ydelta is None or ydelta <= 1):
        tier = "high"
    elif title_tier == "exact":
        tier = "medium"
    else:
        tier = "low"
    if ydelta and ydelta > 1:
        reasons.append(f"year {local_year} vs {cand_year}")
    return tier, reasons


def pick_best(local, candidates):
    scored = []
    for c in candidates:
        tier, why = score_candidate(local, c)
        if tier:
            scored.append((tier, c, why))
    if not scored:
        return None, 0
    order = {"high": 0, "medium": 1, "low": 2}
    scored.sort(key=lambda s: (order[s[0]], -(s[1].get("score") or 0)))
    distinct = {c.get("id") for _, c, _ in scored}
    return scored[0], len(distinct)


# ── data ────────────────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
nd_url = con.execute("SELECT navidrome_url FROM users ORDER BY id LIMIT 1").fetchone()[0]
nd_user, nd_pass = con.execute(
    "SELECT username, navidrome_password FROM users ORDER BY id LIMIT 1").fetchone()
lid_url, lid_key = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()


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

def local_of(a):
    return {"name": a.get("name") or "",
            "artist": a.get("albumArtist") or a.get("artist") or "",
            "year": a.get("year"), "songs": a.get("songCount"),
            "nd_id": a.get("id")}

untagged = [local_of(a) for a in albums if not a.get("mbzReleaseGroupId")]
print(f"albums {len(albums)}  untagged {len(untagged)}  "
      f"distinct untagged artists {len({norm_artist(x['artist']) for x in untagged})}")

# ── A. lidarr oracle ────────────────────────────────────────────────────────
lid = http_json(f"{lid_url.rstrip('/')}/api/v1/album?apikey={lid_key}")
exact, loose = {}, {}
for la in lid:
    mbid = la.get("foreignAlbumId")
    if not mbid:
        continue
    artist = (la.get("artist") or {}).get("artistName") or ""
    exact.setdefault(f"{norm_artist(artist)}|{norm_title(la.get('title') or '')}", set()).add(mbid)
    loose.setdefault(f"{norm_artist(artist)}|{loose_title(la.get('title') or '')}", set()).add(mbid)

multi_exact = sum(1 for v in exact.values() if len(v) > 1)
multi_loose = sum(1 for v in loose.values() if len(v) > 1)
oracle = {"exact_unique": 0, "exact_ambiguous": 0, "loose_unique": 0}
for a in untagged:
    k = f"{norm_artist(a['artist'])}|{norm_title(a['name'])}"
    if k in exact:
        if len(exact[k]) == 1:
            oracle["exact_unique"] += 1
        else:
            oracle["exact_ambiguous"] += 1
    else:
        lk = f"{norm_artist(a['artist'])}|{loose_title(a['name'])}"
        if lk in loose and len(loose[lk]) == 1:
            oracle["loose_unique"] += 1
print(f"\n[A] lidarr oracle: unique {oracle['exact_unique']}, ambiguous {oracle['exact_ambiguous']}, "
      f"loose {oracle['loose_unique']}  "
      f"({(oracle['exact_unique'] + oracle['loose_unique']) / len(untagged) * 100:.1f}% of untagged)")
print(f"    folded keys mapping to >1 RG: exact {multi_exact}, loose {multi_loose}")

# ── B/C. artist-browse vs per-album search ──────────────────────────────────
by_artist = {}
for a in untagged:
    by_artist.setdefault(norm_artist(a["artist"]), []).append(a)
pool = [k for k, v in by_artist.items() if k and len(v) <= 40]
random.seed(11)
random.shuffle(pool)
artists = pool[:ARTIST_SAMPLE]

b_high = b_med = b_low = b_amb = b_artist_missing = 0
c_high = c_none = 0
b_albums = 0
examples, misses = [], []

for key in artists:
    group = by_artist[key]
    display = group[0]["artist"]
    found = mb.get("artist", query=f'artist:"{display}"', limit=5)
    cand_artists = found.get("artists", []) if not found.get("_error") else []
    exact_artist = next((x for x in cand_artists if norm_artist(x.get("name")) == key), None)
    if not exact_artist:
        b_artist_missing += len(group)
        misses.append((display, group[0]["name"], "artist not resolved"))
        continue
    am = exact_artist["id"]
    browse = mb.get("release-group", artist=am, type="album", limit=100)
    rgs = browse.get("release-groups", []) if not browse.get("_error") else []

    for a in group:
        b_albums += 1
        best, distinct = pick_best(a, rgs)
        if best is None:
            misses.append((a["artist"], a["name"], "no match in discography"))
        else:
            tier, cand, why = best
            if distinct > 1:
                b_amb += 1
            if tier == "high":
                b_high += 1
            elif tier == "medium":
                b_med += 1
            else:
                b_low += 1
            if len(examples) < 12:
                examples.append((f"{a['artist']} — {a['name']}",
                                 f"{credits_name(cand.get('artist-credit'))} — {cand.get('title')}",
                                 tier, cand.get("first-release-date"), why))

        # C: per-album fielded search on the same album
        res = mb.get("release-group", query=f'artist:"{a["artist"]}" AND release:"{a["name"]}"',
                     limit=5)
        crs = res.get("release-groups", []) if not res.get("_error") else []
        best_c, _ = pick_best(a, crs)
        if best_c:
            c_high += 1
        else:
            c_none += 1

print(f"\n[B] artist-browse on {b_albums} albums ({len(artists)} artists): "
      f"high {b_high}, medium {b_med}, low {b_low}, ambiguous {b_amb}, "
      f"unresolved artists {b_artist_missing} albums")
if b_albums:
    print(f"    coverage high {b_high / b_albums * 100:.1f}%, "
          f"high+medium {(b_high + b_med) / b_albums * 100:.1f}%")
print(f"[C] per-album fielded search on the same {b_albums} albums: "
      f"high {c_high} ({c_high / max(b_albums, 1) * 100:.1f}%), none {c_none}")

print("\n-- artist-browse matches --")
for local, match, tier, date, why in examples:
    print(f"  [{tier}] {local}  ->  {match} ({date or '?'}){'  # ' + '; '.join(why) if why else ''}")
print("\n-- misses --")
for artist, name, why in misses[:12]:
    print(f"  {artist} — {name}: {why}")

# projected coverage using the artist-browse hit rate on the un-oracle'd slice
oracle_n = oracle["exact_unique"] + oracle["loose_unique"]
remaining = len(untagged) - oracle_n
proj_high = b_high / max(b_albums, 1) * remaining
proj_hm = (b_high + b_med) / max(b_albums, 1) * remaining
base = len(albums) - len(untagged)
print(f"\nprojection (base {base} tagged, oracle +{oracle_n}, remaining {remaining}):")
print(f"  oracle only:            {base + oracle_n} = {(base + oracle_n) / len(albums) * 100:.1f}%")
print(f"  + artist-browse high:   {base + oracle_n + proj_high:.0f} = "
      f"{(base + oracle_n + proj_high) / len(albums) * 100:.1f}%")
print(f"  + high+medium:          {base + oracle_n + proj_hm:.0f} = "
      f"{(base + oracle_n + proj_hm) / len(albums) * 100:.1f}%")
print(f"\nmusicbrainz calls {mb.calls}, throttle retries {mb.retries}")

json.dump({"bands": {"albums": len(albums), "untagged": len(untagged),
                     "untagged_artists": len({norm_artist(x['artist']) for x in untagged})},
           "oracle": oracle, "oracle_multi_exact": multi_exact,
           "artist_browse": {"albums": b_albums, "high": b_high, "medium": b_med,
                             "low": b_low, "ambiguous": b_amb,
                             "unresolved_artist_albums": b_artist_missing},
           "fielded": {"albums": b_albums, "high": c_high, "none": c_none},
           "mb_calls": mb.calls, "retries": mb.retries,
           "examples": examples, "misses": misses},
          open(OUT, "w"), indent=1, ensure_ascii=False)
print(f"detail: {OUT}")
