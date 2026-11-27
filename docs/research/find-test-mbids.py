"""Find MusicBrainz release-group MBIDs for Phase 0 spike test cases.

Read-only lookups: MB, Cover Art Archive, Deezer, Lidarr MBID path.
No secrets printed; credentials come from env where needed.
"""
import json
import sys
import urllib.error
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"
OUT = []


def get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def show(title, rows):
    OUT.append(f"\n=== {title} ===")
    for r in rows:
        artist_credit = r.get("artist-credit") or []
        ac = []
        for a in artist_credit:
            if isinstance(a, dict):
                artist = a.get("artist") or {}
                ac.append(f"{a.get('name')}|{artist.get('id')}")
        OUT.append(
            f"  {r['id']} | {r.get('first-release-date')} | {r.get('primary-type')} "
            f"| {r.get('title')} | {'; '.join(ac)}"
        )


def search_mb(query, limit=10):
    import urllib.parse
    q = urllib.parse.quote(query)
    st, body = get(f"https://musicbrainz.org/ws/2/release-group/?query={q}&fmt=json&limit={limit}")
    if st != 200:
        OUT.append(f"\n=== MB search '{query}' -> HTTP {st} ===")
        OUT.append(f"  {body[:300]}")
        return
    d = json.loads(body)
    OUT.append(f"\n=== MB search '{query}' (count={d.get('count')}) ===")
    show(title=query, rows=d.get("release-groups", []))


# Shang-Chi soundtrack release groups
search_mb('releasegroup:"Shang-Chi and the Legend of the Ten Rings"', 10)
# PSY 9th / Korean-titled albums
search_mb("artist:PSY AND releasegroup:9th", 10)
search_mb("artist:PSY AND primarytype:album", 10)
# BTS Korean-titled
search_mb("artist:BTS AND primarytype:album", 10)
# KPop Demon Hunters soundtrack
search_mb('releasegroup:"KPop Demon Hunters"', 10)
# TROLLS soundtrack
search_mb('releasegroup:TROLLS', 10)

# Deezer check for a Korean-titled album title
import urllib.parse
st, body = get(
    f"https://api.deezer.com/search/album?q={urllib.parse.quote('싸다9')}&limit=5"
)
OUT.append(f"\n=== Deezer search '싸다9' -> HTTP {st} ===")
if st == 200:
    d = json.loads(body)
    for r in d.get("data", []):
        OUT.append(f"  album {r.get('id')} | {r.get('title')} | {r.get('artist', {}).get('name')} | mbid field? {'mbid' in r}")

sys.stdout.write("\n".join(OUT))
