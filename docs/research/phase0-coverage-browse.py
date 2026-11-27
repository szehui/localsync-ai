"""Phase 0 tasks 0.2 and 0.3.

0.2 — measure release-group MBID coverage across the whole Navidrome library via
      the native REST API (batched, no per-album calls).
0.3 — pin down MusicBrainz discography browse semantics (type filters, paging).
"""
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"


def nav_creds():
    con = sqlite3.connect("/app/backend/data/localsync.db")
    row = con.execute(
        "SELECT navidrome_url, username, navidrome_password FROM users "
        "WHERE is_owner=1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        row = con.execute(
            "SELECT navidrome_url, username, navidrome_password FROM users ORDER BY id LIMIT 1").fetchone()
    return row


def call(base, path, method="GET", body=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-nd-authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print("=" * 72)
print("TASK 0.2 — full-library release-group MBID coverage")
print("=" * 72)
url, user, pw = nav_creds()
base = url.rstrip("/")
st, tok = call(base, "/auth/login", "POST", {"username": user, "password": pw})
token = tok.get("token") if isinstance(tok, dict) else None
print(f"  native login: HTTP {st}, token={'yes' if token else 'no'}")
if not token:
    raise SystemExit("native API login failed")

page_size = 200
seen = with_rg = with_album = mismatch = with_date = 0
samples = []
offset = 0
pages = 0
while True:
    st, rows = call(base, f"/api/album?_start={offset}&_end={offset + page_size}&_sort=name",
                    token=token, timeout=60)
    if st != 200 or not isinstance(rows, list):
        print(f"  page at {offset}: HTTP {st} {str(rows)[:120]}")
        break
    if not rows:
        break
    pages += 1
    for a in rows:
        seen += 1
        rg = a.get("mbzReleaseGroupId") or ""
        alb = a.get("mbzAlbumId") or ""
        if rg:
            with_rg += 1
        if alb:
            with_album += 1
        if rg and alb and rg != alb:
            mismatch += 1
        if a.get("releaseDate") or a.get("originalDate"):
            with_date += 1
    if len(samples) < 5:
        samples.extend({"id": a.get("id"), "name": a.get("name"),
                        "rg": a.get("mbzReleaseGroupId"), "album": a.get("mbzAlbumId")}
                       for a in rows[: 5 - len(samples)])
    offset += page_size
    if len(rows) < page_size:
        break
    time.sleep(0.05)

pct = (100.0 * with_rg / seen) if seen else 0.0
print(f"  pages={pages} albums={seen}")
print(f"  with mbzReleaseGroupId: {with_rg} ({pct:.1f}%)")
print(f"  with mbzAlbumId:        {with_album}")
print(f"  both present and different: {mismatch}  (confirms release != release-group)")
print(f"  with a release date:    {with_date}")
print("  samples:")
for s in samples:
    print(f"     {str(s['name'])[:38]:40} rg={s['rg'] or '-'} album={s['album'] or '-'}")

with open("/tmp/rg-coverage.json", "w") as fh:
    json.dump({"total": seen, "with_rg": with_rg, "with_album": with_album,
               "mismatch": mismatch, "with_date": with_date}, fh)

print()
print("=" * 72)
print("TASK 0.3 — MusicBrainz discography browse semantics")
print("=" * 72)


def mb(path):
    req = urllib.request.Request(f"https://musicbrainz.org/ws/2/{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


DAFT = "056e4f3e-d505-4dad-8ec1-d04f521cbb56"
ENHYPEN_ARTIST = None

for type_ in ("album", "ep", "single"):
    st, d = mb(f"release-group?artist={DAFT}&type={type_}&limit=100&inc=artist-credits&fmt=json")
    total = d.get("release-group-count") if st == 200 else "-"
    print(f"  Daft Punk type={type_:7} HTTP {st} total={total} returned={len(d.get('release-groups', [])) if st == 200 else '-'}")
    time.sleep(1.2)

# repeated type params in one call
st, d = mb(f"release-group?artist={DAFT}&type=album&type=ep&limit=100&fmt=json")
print(f"  repeated type params: HTTP {st} total={d.get('release-group-count') if st == 200 else '-'}")

# paging sanity on a prolific artist
st, d = mb(f"artist/?query={urllib.parse.quote('artist:ENHYPEN')}&fmt=json&limit=3")
artist_mbid = None
if st == 200:
    for a in d.get("artists", []):
        if (a.get("name") or "").upper() == "ENHYPEN":
            artist_mbid = a["id"]
            break
print(f"  ENHYPEN artist mbid: {artist_mbid}")

if artist_mbid:
    for offset in (0, 100):
        st, d = mb(f"release-group?artist={artist_mbid}&type=album&limit=100&offset={offset}&fmt=json")
        n = len(d.get("release-groups", [])) if st == 200 else "-"
        total = d.get("release-group-count") if st == 200 else "-"
        print(f"  ENHYPEN albums offset={offset}: HTTP {st} total={total} returned={n}")
        time.sleep(1.2)

print()
print("soundtrack secondary-type check (Shang-Chi songs album):")
st, d = mb("release-group/512af5da-2aef-41df-95ee-3e856bcacbc5?inc=artist-credits&fmt=json")
if st == 200:
    print(f"  title={d.get('title')} primary={d.get('primary-type')} "
          f"secondary={d.get('secondary-types')} "
          f"artist={[a.get('name') for a in d.get('artist-credit', []) if isinstance(a, dict)]}")
