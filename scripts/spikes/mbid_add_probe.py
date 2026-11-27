"""Phase 0 spike: prove Lidarr MBID add path end-to-end.

Read-only browsing + temporary add/verify/cleanup against live Lidarr.
Credentials from env LIDARR_URL / LIDARR_API_KEY.
Nothing is left behind: every created album/artist is removed afterwards.
"""
import asyncio
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

UA = "localsync-research/0.1 ( https://github.com/szehui/localsync-ai )"


def _credentials():
    url = os.environ.get("LIDARR_URL")
    key = os.environ.get("LIDARR_API_KEY")
    if url and key:
        return url.rstrip("/"), key
    # Fallback for running inside the localsync container: read the saved config.
    # The key is used, never printed.
    con = sqlite3.connect("/app/backend/data/localsync.db")
    row = con.execute("SELECT url, api_key FROM lidarr_config LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no LIDARR_URL/LIDARR_API_KEY in env and no lidarr_config row")
    return row[0].rstrip("/"), row[1]


BASE, KEY = _credentials()

CASES = [
    ("daft-punk-ram", "aa997ea0-2936-40bd-884d-3af8a0e064dc"),
    ("shang-chi-score", "c4c9da48-50d3-41dd-9384-813865ac451c"),        # Joel P West
    ("shang-chi-songs", "512af5da-2aef-41df-95ee-3e856bcacbc5"),   # Various Artists
    ("psy-9th-korean", "8ef6391e-2256-4f7f-b490-32faa29af084"),    # 싸다9 (Korean title)
]


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}/api/v1/{path}",
        data=data,
        headers={"X-Api-Key": KEY, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def log(label, detail):
    print(f"[{label}] {detail}")


def find_artist_albums(artist_id):
    st, body = http("GET", f"album?artistId={artist_id}")
    if st != 200:
        log("artist-albums", f"HTTP {st} {body}")
        return []
    rows = json.loads(body)
    return [r for r in rows if isinstance(r, dict)]


def unmonitor_artist_albums(artist_id, keep_ids):
    """Unmonitor every album under artist except keep_ids; verify by re-read."""
    keep = set(keep_ids)
    for attempt in range(4):
        rows = find_artist_albums(artist_id)
        extra = [r["id"] for r in rows if r.get("monitored") and r["id"] not in keep]
        if not extra:
            return True
        body = json.dumps({"albumIds": extra, "monitored": False})
        st, txt = http("PUT", "album/monitor", {"albumIds": extra, "monitored": False})
        if st not in (200, 204):
            log("collapse", f"PUT HTTP {st} {txt}")
        log("collapse", f"attempt {attempt+1}: unmonitored {len(extra)}")
    rows = find_artist_albums(artist_id)
    still = [r["id"] for r in rows if r.get("monitored") and r["id"] not in keep]
    return not still


def delete_album(album_id):
    st, txt = http("DELETE", f"album/{album_id}")
    return st in (200, 204)


def delete_artist(artist_id):
    st, txt = http("DELETE", f"artist/{artist_id}")
    return st in (200, 204)


async def spike(label, mbid):
    log(label, f"1. lookup term=mbid:{mbid}")
    st, body = http("GET", f"album/lookup?term=mbid:{mbid}")
    if st != 200:
        log(label, f"lookup HTTP {st}: {body}")
        return {"label": label, "mbid": mbid, "outcome": f"lookup-http-{st}"}
    rows = json.loads(body)
    matches = [r for r in rows if isinstance(r, dict) and (r.get("foreignAlbumId") or "").lower() == mbid.lower()]
    if not matches:
        log(label, f"lookup returned {len(rows)} row(s), none match MBID {mbid}")
        return {"label": label, "mbid": mbid, "outcome": f"no-match-{len(rows)}"}
    album = matches[0]
    artist_name = (album.get("artist") or {}).get("artistName") or "Unknown"
    foreign_artist = (album.get("artist") or {}).get("foreignArtistId")
    log(label, f"2. resolved '{album.get('title')}' by {artist_name} "
               f"(artist MBID {foreign_artist}, type {album.get('albumType')}, "
               f"{len(album.get('releases') or [])} releases)")

    # Never mutate an existing library row: the spike only proves that a *new*
    # album can be created and collapsed, then removes it again.
    st, existing_raw = http("GET", f"album?foreignAlbumId={mbid}")
    if st == 200:
        try:
            existing_rows = json.loads(existing_raw)
        except json.JSONDecodeError:
            existing_rows = []
        if isinstance(existing_rows, list) and existing_rows:
            row = existing_rows[0]
            log(label, f"   already in Lidarr (album {row.get('id')}, "
                       f"monitored={row.get('monitored')}) — skipping add, no state change")
            return {"label": label, "mbid": mbid, "outcome": "already-present",
                    "title": album.get("title"), "artist": artist_name}

    log(label, "3. defaults (quality/metadata profiles + root folder)")
    st, q = http("GET", "qualityprofile")
    st2, m = http("GET", "metadataprofile")
    st3, r = http("GET", "rootfolder")
    if not (st == 200 and st2 == 200 and st3 == 200):
        log(label, f"defaults HTTP {st}/{st2}/{st3}")
        return {"label": label, "mbid": mbid, "outcome": "defaults-failed"}
    defaults = {
        "quality_profile_id": json.loads(q)[0]["id"],
        "metadata_profile_id": json.loads(m)[0]["id"],
        "root_folder": json.loads(r)[0]["path"],
    }

    log(label, "4. POST /album (monitored=true, no search)")
    payload = dict(album)
    artist = dict(album.get("artist") or {})
    artist.update({
        "qualityProfileId": defaults["quality_profile_id"],
        "metadataProfileId": defaults["metadata_profile_id"],
        "rootFolderPath": defaults["root_folder"],
        "monitored": False,
        "monitorNewItems": "none",
        "addOptions": {"monitor": "none", "searchForMissingAlbums": False, "albumsToMonitor": []},
    })
    payload.update({
        "monitored": True,
        "addOptions": {"addType": "manual", "searchForNewAlbum": False},
        "artist": artist,
    })
    st, created = http("POST", "album", json.loads(json.dumps(payload)))
    if st not in (200, 201):
        log(label, f"POST HTTP {st}: {created}")
        return {"label": label, "mbid": mbid, "outcome": f"post-http-{st}"}
    created_obj = json.loads(created)
    new_album_id = created_obj.get("id")
    new_artist_id = created_obj.get("artistId")
    if not new_album_id or not new_artist_id:
        log(label, f"POST no ids: {created_obj}")
        return {"label": label, "mbid": mbid, "outcome": "no-ids"}
    log(label, f"   created album {new_album_id}, artist {new_artist_id}")

    log(label, "5. collapse monitoring to this album only")
    ok = unmonitor_artist_albums(new_artist_id, [new_album_id])
    log(label, f"   collapse {'ok' if ok else 'FAILED'}")

    log(label, "6. re-read artist monitored set")
    rows = find_artist_albums(new_artist_id)
    monitored = [r["id"] for r in rows if r.get("monitored")]
    log(label, f"   artist monitors: {monitored} (expected [{new_album_id}])")

    # ── cleanup: remove what we created ──
    log(label, "7. cleanup: delete album")
    st, _ = http("DELETE", f"album/{new_album_id}")
    log(label, f"   album delete HTTP {st}")
    # Delete the artist only if we created it and it now owns no albums at all.
    rows = find_artist_albums(new_artist_id)
    if not rows:
        st, _ = http("DELETE", f"artist/{new_artist_id}")
        log(label, f"   artist delete HTTP {st} (artist had no remaining albums)")
    else:
        log(label, f"   artist keep ({len(rows)} album row(s) remain)")

    return {
        "label": label,
        "mbid": mbid,
        "outcome": "ok",
        "title": album.get("title"),
        "artist": artist_name,
        "type": album.get("albumType"),
        "releases": len(album.get("releases") or []),
        "monitored_after_collapse": monitored,
    }


async def main():
    results = []
    for label, mbid in CASES:
        try:
            results.append(await spike(label, mbid))
        except Exception as e:  # noqa: BLE001
            results.append({"label": label, "mbid": mbid, "outcome": f"exception {type(e).__name__}: {e}"})
    print("\n========== SPIKE SUMMARY ==========")
    for r in results:
        print(f"  {r['label']}: {r['outcome']}" + (f" ({r.get('title')} by {r.get('artist')})" if r.get("title") else ""))


if __name__ == "__main__":
    asyncio.run(main())
