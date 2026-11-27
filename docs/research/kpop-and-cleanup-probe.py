"""Phase 0 spike follow-ups.

1. Inspect whatever the add spike left behind (artists 503 / 504) and clean up
   metadata-only leftovers (zero files, nothing monitored).
2. Probe how a Latin-script query reaches a Korean-titled release group:
   Lidarr/SkyHook search, MusicBrainz search, artist aliases, Deezer.
"""
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"


def creds():
    url, key = os.environ.get("LIDARR_URL"), os.environ.get("LIDARR_API_KEY")
    if url and key:
        return url.rstrip("/"), key
    con = sqlite3.connect("/app/backend/data/localsync.db")
    row = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()
    return row[0].rstrip("/"), row[1]


BASE, KEY = creds()


def lidarr(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}/api/v1/{path}", data=data,
        headers={"X-Api-Key": KEY, "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def mb(path):
    req = urllib.request.Request(f"https://musicbrainz.org/ws/2/{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def deezer(path):
    req = urllib.request.Request(f"https://api.deezer.com/{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print("=" * 72)
print("1. leftovers from the add spike")
print("=" * 72)
for artist_id in (503, 504):
    st, artist = lidarr("GET", f"artist/{artist_id}")
    if st != 200:
        print(f"  artist {artist_id}: HTTP {st}")
        continue
    stats = artist.get("statistics") or {}
    print(f"  artist {artist_id}: {artist.get('artistName')} | monitored={artist.get('monitored')} "
          f"| albums={stats.get('albumCount')} | files={stats.get('trackFileCount')}")
    st, albums = lidarr("GET", f"album?artistId={artist_id}")
    for a in albums or []:
        s = a.get("statistics") or {}
        print(f"     album {a.get('id')}: {a.get('title')[:50]:52} monitored={a.get('monitored')} "
              f"files={s.get('trackFileCount')}/{s.get('trackCount')}")
    # Metadata-only leftover: nothing on disk, nothing monitored -> safe to drop.
    files = stats.get("trackFileCount") or 0
    monitored = [a for a in (albums or []) if a.get("monitored")]
    if files == 0 and not monitored:
        dst, deleted = lidarr("DELETE", f"artist/{artist_id}")
        print(f"     -> cleanup delete artist {artist_id}: HTTP {dst}")
    else:
        print(f"     -> kept (files={files}, monitored={len(monitored)})")

print()
print("=" * 72)
print("2. can a Latin query reach 싸다9 (PSY 9th)?")
print("=" * 72)
for term in ("PSY 9th", "PSY 싸다9", "싸다9", "psy9th"):
    st, rows = lidarr("GET", f"album/lookup?term={urllib.parse.quote(term)}")
    if not isinstance(rows, list):
        print(f"  Lidarr lookup {term!r}: HTTP {st} {rows}")
        continue
    print(f"  Lidarr lookup {term!r}: {len(rows)} row(s)")
    for r in rows[:4]:
        artist = (r.get("artist") or {}).get("artistName")
        hit = " <== TARGET" if r.get("foreignAlbumId") == "8ef6391e-2256-4f7f-b490-32faa29af084" else ""
        print(f"     {r.get('title')[:44]:46} | {artist} | {r.get('foreignAlbumId')}{hit}")

for q in ("artist:PSY AND releasegroup:9th", "PSY 9th", "artist:PSY%20AND%20primarytype:album"):
    st, d = mb(f"release-group/?query={urllib.parse.quote(q)}&fmt=json&limit=5")
    if st != 200:
        print(f"  MB search {q!r}: HTTP {st}")
        continue
    target = [r for r in d.get("release-groups", [])
              if r["id"] == "8ef6391e-2256-4f7f-b490-32faa29af084"]
    print(f"  MB search {q!r}: count={d.get('count')} target_in_top5={bool(target)} "
          f"top={[r.get('title') for r in d.get('release-groups', [])[:3]]}")

st, artist = mb("artist/f99b7d67-4e63-4678-aa66-4c6ac0f7d24a?inc=aliases&fmt=json")
print(f"  MB artist aliases: {[(a.get('name'), a.get('locale'), a.get('type')) for a in artist.get('aliases', [])]}")

for q in ("PSY 9th", "싸다9"):
    st, d = deezer(f"search/album?q={urllib.parse.quote(q)}&limit=5")
    print(f"  Deezer search {q!r}: HTTP {st}")
    for r in (d or {}).get("data", [])[:5]:
        print(f"     {r.get('title')[:44]:46} | {r.get('artist', {}).get('name')} "
              f"| {r.get('release_date')} | {r.get('nb_tracks')} tracks")
