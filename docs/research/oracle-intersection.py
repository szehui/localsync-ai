"""Decisive number: of the albums that are actually worth backfilling
(multi-track, parseable title), how many does the Lidarr oracle already cover?
"""
import json
import re
import sqlite3
import unicodedata
import urllib.request

UA = "localsync-research/0.1"
DB = "/app/backend/data/localsync.db"

con = sqlite3.connect(DB)
nd_url, nd_user, nd_pass = con.execute(
    "SELECT navidrome_url, username, navidrome_password FROM users ORDER BY id LIMIT 1").fetchone()
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

ED = re.compile(r"deluxe|remaster|expanded|anniversary|bonus|special|reissue|collector|"
                r"limited|explicit|clean|mono|stereo|edition|version", re.I)


def norm(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("&", " and ")

    def br(m):
        return " " if ED.search(m.group(1)) else f" {m.group(1)} "

    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", br, t)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t)
    return " ".join(t.split())


def loose(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("&", " and ")
    t = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", " ", t)
    return " ".join(re.sub(r"[^0-9a-z]+", " ", t).split())


def na(t):
    return " ".join(re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+", " ", t.lower()).split())


UNKNOWN = re.compile(r"^\[?\s*(unknown album|unknown|untitled|no album)\s*\]?$", re.I)

def garbled(t):
    if "\ufffd" in t:
        return True
    nonlatin = sum(1 for c in t if ord(c) > 0x2E80)
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    return bool(t) and latin and 0 < nonlatin < 3


lid = http_json(f"{lid_url.rstrip('/')}/api/v1/album?apikey={lid_key}")
exact, loose_map = {}, {}
for la in lid:
    m = la.get("foreignAlbumId")
    if not m:
        continue
    ar = (la.get("artist") or {}).get("artistName") or ""
    exact.setdefault(f"{na(ar)}|{norm(la.get('title') or '')}", set()).add(m)
    loose_map.setdefault(f"{na(ar)}|{loose(la.get('title') or '')}", set()).add(m)

untagged = [a for a in albums if not a.get("mbzReleaseGroupId")]
target = [a for a in untagged
          if (a.get("songCount") or 0) > 1
          and not UNKNOWN.match((a.get("name") or "").strip())
          and not garbled(a.get("name") or "")]

hit, amb, miss = [], [], []
for a in target:
    k = f"{na(a.get('albumArtist') or a.get('artist') or '')}|{norm(a.get('name') or '')}"
    if k in exact:
        (hit if len(exact[k]) == 1 else amb).append(a)
        continue
    lk = f"{na(a.get('albumArtist') or a.get('artist') or '')}|{loose(a.get('name') or '')}"
    if lk in loose_map and len(loose_map[lk]) == 1:
        hit.append(a)
    else:
        miss.append(a)

print(f"library {len(albums)}, tagged {len(albums) - len(untagged)}, untagged {len(untagged)}")
print(f"backfill target (multi-track, parseable): {len(target)}")
print(f"  oracle covered:      {len(hit)}  ({len(hit) / len(target) * 100:.1f}% of target)")
print(f"  oracle ambiguous:    {len(amb)}")
print(f"  still unmapped:      {len(miss)}")

print(f"\nstill-unmapped artists: {len({na(a.get('albumArtist') or '') for a in miss})}")
print(f"still-unmapped, Various Artists: "
      f"{sum(1 for a in miss if 'various' in (a.get('albumArtist') or '').lower())}")

print("\n-- remaining examples --")
for a in miss[:15]:
    d = (a.get("date") or "")[:10] or a.get("maxYear") or "?"
    print(f"  {(a.get('albumArtist') or '?')[:40]:40s} — {(a.get('name') or '')[:45]:45s} "
          f"[{d}, {a.get('songCount')} tr]")

# what coverage would we reach
proj = len(albums) - len(untagged) + len(hit)
print(f"\ncoverage with oracle applied to target: {proj}/{len(albums)} = {proj / len(albums) * 100:.1f}%")
print(f"coverage if every remaining target album were also resolved: "
      f"{proj + len(miss)}/{len(albums)} = {(proj + len(miss)) / len(albums) * 100:.1f}%")

json.dump({"library": len(albums), "untagged": len(untagged), "target": len(target),
           "oracle_hit": len(hit), "oracle_amb": len(amb), "remaining": len(miss),
           "remaining_artists": len({na(a.get('albumArtist') or '') for a in miss}),
           "remaining_va": sum(1 for a in miss if 'various' in (a.get('albumArtist') or '').lower()),
           "remaining_examples": [{"artist": a.get("albumArtist"), "name": a.get("name"),
                                   "date": a.get("date"), "songs": a.get("songCount")}
                                  for a in miss[:40]]},
          open("/tmp/oracle-intersection.json", "w"), indent=1, ensure_ascii=False)
