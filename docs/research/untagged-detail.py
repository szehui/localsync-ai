"""Do we have any usable date signal, and how much of the untagged slice is
actually matchable? Also counts the local title-junk patterns that break
fielded search."""
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

untagged = [a for a in albums if not a.get("mbzReleaseGroupId")]
tagged = [a for a in albums if a.get("mbzReleaseGroupId")]

def has(a, k):
    return bool(a.get(k))

print("date fields on the album object:")
print(f"  keys: {sorted(untagged[0].keys())}")
for field in ("year", "releaseDate", "originalDate", "date"):
    t = sum(1 for a in untagged if has(a, field))
    g = sum(1 for a in tagged if has(a, field))
    print(f"  {field:13s} untagged {t:5d}/{len(untagged)}   tagged {g:5d}/{len(tagged)}")

LEAD_JUNK = re.compile(r"^\s*(\(\s*\d{1,4}\s*\)|\[\s*\d{1,4}\s*\]|\d{1,4}\s*[-–.)]\s+|[-–—]\s+)")
YEAR_ONLY = re.compile(r"[\(\[]\s*(19|20)\d{2}\s*[\)\]]")
UNKNOWN = re.compile(r"^\[?\s*(unknown album|unknown|untitled|no album)\s*\]?$", re.I)

junk_lead = sum(1 for a in untagged if LEAD_JUNK.match(a.get("name") or ""))
year_only = sum(1 for a in untagged if YEAR_ONLY.search(a.get("name") or ""))
unknown = sum(1 for a in untagged if UNKNOWN.match((a.get("name") or "").strip()))
multi = [a for a in untagged if (a.get("songCount") or 0) > 1]
single = [a for a in untagged if (a.get("songCount") or 0) <= 1]

print("\ntitle junk patterns in untagged:")
print(f"  leading (year)/(num)/dash: {junk_lead}")
print(f"  embedded (19xx)/(20xx):    {year_only}")
print(f"  literal [Unknown Album]:   {unknown}")
print(f"  multi-track untagged:      {len(multi)}")
print(f"  single-track untagged:     {len(single)}")

# mojibake more honestly: replacement char, or mostly non-ascii symbols
def looks_mojibake(t):
    if "\ufffd" in t:
        return True
    nonlatin = sum(1 for c in t if ord(c) > 0x2E80)
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    symbols = sum(1 for c in t if not c.isalnum() and not c.isspace())
    return bool(t) and (symbols / len(t) > 0.3 or (latin and nonlatin and nonlatin < 3))

bad = [a for a in multi if looks_mojibake(a.get("name") or "")]
print(f"  likely mojibake/garbled in multi-track: {len(bad)}")

target = [a for a in multi if not looks_mojibake(a.get("name") or "")
          and not UNKNOWN.match((a.get("name") or "").strip())]
print(f"\nrealistic backfill target: {len(target)} albums "
      f"({len(target) / len(albums) * 100:.1f}% of library)")
print("  of which Various Artists:",
      sum(1 for a in target if "various" in ((a.get("albumArtist") or "").lower())))

json.dump({"date_fields": {f: sum(1 for a in untagged if has(a, f))
                          for f in ("year", "releaseDate", "originalDate")},
           "junk_lead": junk_lead, "year_only": year_only, "unknown": unknown,
           "multi": len(multi), "single": len(single), "mojibake": len(bad),
           "target": len(target)},
          open("/tmp/untagged-detail.json", "w"), indent=1)
