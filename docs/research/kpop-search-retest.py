"""Retest non-Latin title search properly (quoted / fielded), and verify the
track-count signal that alias enrichment would key on."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"


def mb(path):
    req = urllib.request.Request(f"https://musicbrainz.org/ws/2/{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


queries = [
    'releasegroup:"화양연화 pt.1"',
    "releasegroup:(화양연화)",
    'releasegroup:"싸다9"',
    "releasegroup:(싸다9)",
    "화양연화",
]
for q in queries:
    st, d = mb(f"release-group/?query={urllib.parse.quote(q)}&fmt=json&limit=3")
    rows = d.get("release-groups", []) if st == 200 else []
    print(f"{q:32} HTTP {st} count={d.get('count') if st == 200 else '-'} "
          f"-> {[r.get('title') for r in rows]}")
    time.sleep(1.2)

print()
print("track counts for 싸다9 (MB) — release-group releases:")
st, rg = mb("release-group/8ef6391e-2256-4f7f-b490-32faa29af084?inc=releases&fmt=json")
for rel in (rg.get("releases") or [])[:3]:
    print(f"  release {rel.get('id')} | {rel.get('title')} | {rel.get('date')} | status={rel.get('status')}")
    time.sleep(1.2)
    st2, full = mb(f"release/{rel.get('id')}?inc=recordings&fmt=json")
    if st2 == 200:
        tracks = full.get("media") or []
        total = sum(len(m.get("tracks") or []) for m in tracks)
        print(f"     {total} tracks across {len(tracks)} medium/media")
