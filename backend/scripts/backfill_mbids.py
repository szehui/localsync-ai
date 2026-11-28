#!/usr/bin/env python3
"""Standalone job: resolve release-group MBIDs for library albums.

Deliberately not part of the library sync. It is a bulk one-time copy plus a
slow MusicBrainz tail, and a sync should not block on ~400 upstream calls.

Usage (inside the backend container):

    python scripts/backfill_mbids.py                    # all three tiers
    python scripts/backfill_mbids.py --tier oracle      # Lidarr copy only, no MB calls
    python scripts/backfill_mbids.py --tier tail --limit 25
    python scripts/backfill_mbids.py --report           # coverage only, no writes
    python scripts/backfill_mbids.py --retry-failed     # also revisit the known floor

Exit code is 0 when the run completes, even if some albums stay unmatchable —
"no release group exists" is an answer, not a failure.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import Album, LidarrConfig, SessionLocal, User, init_db
from app.services.lidarr import LidarrService
from app.services.mbid_backfill import (
    oracle_tier,
    report,
    tail_tier,
    untagged,
)
from app.services.musicbrainz import MusicBrainzClient
from app.services.navidrome_native import NavidromeNative, sync_album_mbids

logger = logging.getLogger("backfill_mbids")


def _owner(db) -> User | None:
    return db.query(User).filter(User.is_owner.is_(True)).first() or \
        db.query(User).order_by(User.id).first()


def _lidarr(db) -> LidarrService | None:
    config = db.query(LidarrConfig).filter(LidarrConfig.id == 1).first()
    if config is None:
        return None
    return LidarrService(config.url, config.api_key)


async def _tag_tier(db) -> dict:
    owner = _owner(db)
    if owner is None:
        return {"error": "no users configured"}
    native = NavidromeNative(owner.navidrome_url, owner.username,
                             owner.navidrome_password)
    try:
        return await sync_album_mbids(db, native)
    finally:
        await native.close()


async def main(args) -> int:
    init_db()
    db = SessionLocal()
    try:
        if args.report:
            print(json.dumps(report(db), indent=2))
            return 0

        print(f"before: {json.dumps(report(db))}")

        if args.tier in ("tags", "all"):
            print(f"tags: {json.dumps(await _tag_tier(db))}")

        if args.tier in ("oracle", "all"):
            service = _lidarr(db)
            if service is None:
                print("oracle: skipped (Lidarr not configured)")
            else:
                albums = await service.get_all_albums()
                print(f"oracle: {json.dumps(oracle_tier(db, albums))}")

        if args.tier in ("tail", "all"):
            mb = MusicBrainzClient()
            try:
                stats = await tail_tier(db, mb, artist_limit=args.limit,
                                       min_tracks=args.min_tracks)
                print(f"tail: {json.dumps(stats)}")
            finally:
                await mb.close()

        after = report(db)
        print(f"after: {json.dumps(after)}")
        remaining = untagged(db)
        print(f"still unresolved: {len(remaining)} album(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=["all", "tags", "oracle", "tail"],
                        default="all")
    parser.add_argument("--limit", type=int, default=None,
                        help="maximum artists to process in the tail tier")
    parser.add_argument("--min-tracks", type=int, default=2,
                        help="skip albums with fewer tracks (singles rarely resolve)")
    parser.add_argument("--report", action="store_true",
                        help="print coverage and exit without writing")
    parser.add_argument("--retry-failed", action="store_true",
                        help="also revisit albums already marked as attempted")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main(args)))
