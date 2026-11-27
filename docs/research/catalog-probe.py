"""Probe candidate music catalogs for the Seerr-for-music design.

No secrets. MusicBrainz / ListenBrainz / Cover Art Archive / Deezer are all
unauthenticated. Lidarr is probed separately (needs an API key).
"""
import json
import time
import urllib.request
import urllib.error

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
MB = "https://musicbrainz.org/ws/2"
LB = "https://api.listenbrainz.org/1"
DZ = "https://api.deezer.com"
RAM = "aa997ea0-2936-40bd-884d-3af8a0e064dc"  # Daft Punk - Random Access Memories
DP_ARTIST = "056e4f3e-d505-4dad-8ec1-d04f521cbb56"  # Daft Punk


def get(url, accept="application/json", extra_headers=None):
    headers = {"User-Agent": UA, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def section(name):
    print("\n" + "=" * 70)
    print(name)
    print("=" * 70)


def show(payload, keys=None, limit=6):
    try:
        data = json.loads(payload)
    except Exception:
        print(payload[:300])
        return None
    if isinstance(data, list):
        for row in data[:limit]:
            print("  ", {k: row.get(k) for k in keys} if keys else row)
    elif isinstance(data, dict):
        if keys:
            print("  ", {k: data.get(k) for k in keys})
        else:
            print("  ", json.dumps(data)[:400])
    return data


# ---------------------------------------------------------------- MusicBrainz
section("1. MusicBrainz release-group direct lookup by MBID (exact, 1 call)")
st, hdr, body = get(f"{MB}/release-group/{RAM}?inc=artist-credits+releases&fmt=json")
print("status", st)
rg = json.loads(body)
print("  title:", rg.get("title"))
print("  primary/secondary:", rg.get("primary-type"), rg.get("secondary-types"))
print("  first release:", rg.get("first-release-date"))
print("  artist-credit:", [(a.get("name"), (a.get("artist") or {}).get("id"))
                           for a in rg.get("artist-credit", []) if isinstance(a, dict)])
print("  releases:", len(rg.get("releases", [])), "e.g.",
      [(r.get("title"), r.get("status"), r.get("date")) for r in rg.get("releases", [])[:3]])
time.sleep(1.1)

section("2. MusicBrainz release-group SEARCH by rgid:<real uuid> (does the field work?)")
st, hdr, body = get(f"{MB}/release-group/?query=rgid:{RAM}&fmt=json&limit=3")
d = json.loads(body)
print("status", st, "count", d.get("count"),
      "->", [(r["id"], r["title"], r.get("score")) for r in d.get("release-groups", [])])
time.sleep(1.1)

section("3. MusicBrainz artist lookup by MBID + release-group browse")
st, hdr, body = get(f"{MB}/artist/{DP_ARTIST}?inc=url-rels&fmt=json")
artist = json.loads(body)
print("status", st, "name", artist.get("name"), "| country", artist.get("country"),
      "| disambiguation", artist.get("disambiguation"))
time.sleep(1.1)
st, hdr, body = get(f"{MB}/release-group?artist={DP_ARTIST}&inc=artist-credits&fmt=json&limit=5")
d = json.loads(body)
print("browse release-groups status", st, "count", d.get("release-group-count"))
for r in d.get("release-groups", [])[:5]:
    print("   ", r.get("first-release-date"), "|", r.get("primary-type"), "|", r.get("title"))
time.sleep(1.1)

section("4. MusicBrainz search paging + score shape (album query)")
st, hdr, body = get(f"{MB}/release-group/?query=releasegroup:nevermind%20AND%20artist:nirvana&fmt=json&limit=5")
d = json.loads(body)
for r in d.get("release-groups", [])[:5]:
    print("   ", r.get("score"), "|", r.get("first-release-date"), "|", r.get("primary-type"),
          "|", r.get("title"), "|", r["id"])

# ------------------------------------------------------------- Cover Art Archive
section("5. Cover Art Archive availability (release-group front cover)")
st, hdr, body = get(f"https://coverartarchive.org/release-group/{RAM}", accept="application/json")
print("status", st)
try:
    caa = json.loads(body)
    imgs = caa.get("images", [])
    print("  images:", len(imgs), "front:", any(i.get("front") for i in imgs))
    front = next((i for i in imgs if i.get("front")), imgs[0] if imgs else {})
    print("  thumbnails:", list((front.get("thumbnails") or {}).keys()))
except Exception:
    print("  no JSON:", body[:120])

# ---------------------------------------------------------------- ListenBrainz
section("6. ListenBrainz popularity by artist MBIDs (trending / 'what people play')")
st, hdr, body = get(f"{LB}/popularity/release-group?artist_mbids={DP_ARTIST}&count=5")
print("status", st)
try:
    for row in body and json.loads(body):
        print("   ", row)
except Exception:
    print(body[:300])

section("7. ListenBrainz fresh releases (new-music rail)")
st, hdr, body = get(f"{LB}/explore/fresh-releases/?days=7")
print("status", st)
try:
    d = json.loads(body)
    for r in (d.get("releases") or [])[:4]:
        print("   ", r.get("release_date"), "|", r.get("release_name"), "|",
              r.get("artist_credit_name"), "|", r.get("release_group_mbid"))
except Exception:
    print(body[:300])

# ---------------------------------------------------------------------- Deezer
section("8. Deezer album search (free, no key) - result quality / popularity")
st, hdr, body = get(f"{DZ}/search/album?q=daft%20punk%20random%20access%20memories&limit=3")
print("status", st)
try:
    for r in json.loads(body).get("data", []):
        print("   ", r.get("title"), "|", r.get("artist", {}).get("name"),
              "| id", r.get("id"), "| nb_tracks", r.get("nb_tracks"),
              "| cover", bool(r.get("cover_medium")))
except Exception:
    print(body[:300])

section("9. Deezer album detail - does it expose any MusicBrainz id?")
st, hdr, body = get(f"{DZ}/album/302127")
d = json.loads(body)
print("status", st, "| top-level keys:", sorted(d.keys()))
print("  upc:", d.get("upc"), "| label:", d.get("label"), "| release_date:", d.get("release_date"))
print("  any mbid field:", [k for k in d if "mbid" in k.lower()] or "NONE")

section("10. Deezer artist detail - mbid?")
st, hdr, body = get(f"{DZ}/artist/27")
d = json.loads(body)
print("status", st, "| keys:", sorted(d.keys()))
print("  mbid:", d.get("mbid", "MISSING"))
