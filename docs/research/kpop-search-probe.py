"""Does MusicBrainz unfielded search reach Korean-titled release groups via the
common English/romanized name? Three known cases."""
import json
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"

CASES = [
    # (what a user would type, the expected Korean/other-script title fragment)
    ("PSY 9th", "싸다9"),
    ("BTS The Most Beautiful Moment in Life", "화양연화"),
    ("BTS Hwayangyeonhwa", "화양연화"),
    ("BTS WINGS", "WINGS"),
    ("TWICE Formula of Love", "Formula of Love"),
]


def mb(path):
    req = urllib.request.Request(f"https://musicbrainz.org/ws/2/{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print(f"{'query':40} {'count':>7}  hits (top 5)")
print("-" * 110)
for query, expected in CASES:
    st, d = mb(f"release-group/?query={urllib.parse.quote(query)}&fmt=json&limit=5")
    if st != 200:
        print(f"{query:40} HTTP {st}")
        continue
    rows = d.get("release-groups", [])
    rank = next((i + 1 for i, r in enumerate(rows)
                 if expected.lower() in (r.get("title") or "").lower()), None)
    titles = [f"{r.get('title')[:34]}" for r in rows[:5]]
    print(f"{query:40} {d.get('count'):>7}  rank={rank}  {titles}")

print()
print("exact Korean-title query still works:")
for query in ("화양연화 pt.1", "싸다9"):
    st, d = mb(f"release-group/?query={urllib.parse.quote(query)}&fmt=json&limit=3")
    rows = d.get("release-groups", []) if st == 200 else []
    print(f"  {query:16} -> {[r.get('title') for r in rows]}")
