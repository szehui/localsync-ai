"""Library sync service — pulls metadata from Navidrome into local SQLite cache."""
import json
import logging
import re
import unicodedata
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.database import Artist, Album, Track, SessionLocal
from app.services.navidrome import NavidromeClient

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, client: NavidromeClient):
        self.client = client
        self.is_syncing = False
        self.last_sync: datetime | None = None
        self.last_stats: dict | None = None

    async def full_sync(self, db: Session) -> dict:
        """Full library sync: artists → albums → tracks, then prune vanished rows."""
        self.is_syncing = True
        stats = {
            "artists": 0, "albums": 0, "tracks": 0,
            "pruned_artists": 0, "pruned_albums": 0, "pruned_tracks": 0,
        }

        try:
            logger.info("Starting full library sync...")

            # 1. Sync artists
            seen_artist_ids: set = set()
            artists = await self.client.get_artists()
            for artist_data in artists:
                artist = Artist(
                    id=artist_data["id"],
                    name=artist_data.get("name", "Unknown"),
                    album_count=artist_data.get("albumCount", 0),
                    cover_art=artist_data.get("coverArt"),
                    last_synced=datetime.utcnow(),
                )
                db.merge(artist)
                seen_artist_ids.add(artist_data["id"])
                stats["artists"] += 1
            db.commit()
            logger.info(f"Synced {stats['artists']} artists")

            # 2. Sync albums — paginated fetch to handle large libraries
            seen_album_ids: set = set()
            offset = 0
            page_size = 500
            while True:
                albums = await self.client.get_album_list2(type_="alphabeticalByName", size=page_size, offset=offset)
                if not albums:
                    break
                for album_data in albums:
                    album = Album(
                        id=album_data["id"],
                        name=album_data.get("name", "Unknown"),
                        artist_id=album_data.get("artistId"),
                        artist_name=album_data.get("artist"),
                        year=album_data.get("year"),
                        genre=album_data.get("genre"),
                        cover_art=album_data.get("coverArt"),
                        song_count=album_data.get("songCount", 0),
                        play_count=album_data.get("playCount", 0),
                        rating=album_data.get("rating", 0),
                        created_at=_parse_datetime(album_data.get("created")),
                        last_synced=datetime.utcnow(),
                    )
                    db.merge(album)
                    seen_album_ids.add(album_data["id"])
                    stats["albums"] += 1
                db.commit()
                if len(albums) < page_size:
                    break
                offset += page_size
            logger.info(f"Synced {stats['albums']} albums")

            # 3. Sync tracks — iterate the albums Navidrome actually returned
            #    this run (not every album row in the DB, which may be stale).
            seen_track_ids: set = set()
            for album_id in sorted(seen_album_ids):
                try:
                    album_detail = await self.client.get_album(album_id)
                    album = album_detail.get("album", {})
                    for song_data in album.get("song", []):
                        db.merge(_build_track(song_data, album))
                        seen_track_ids.add(song_data["id"])
                        stats["tracks"] += 1
                except Exception as e:
                    logger.warning(f"Failed to sync album {album_id}: {e}")
                    continue
            db.commit()
            logger.info(f"Synced {stats['tracks']} tracks")

            # 4. Prune rows Navidrome no longer returns (children first).
            stats["pruned_tracks"] = _prune(db, Track, seen_track_ids)
            stats["pruned_albums"] = _prune(db, Album, seen_album_ids)
            stats["pruned_artists"] = _prune(db, Artist, seen_artist_ids)
            if any(stats[k] for k in ("pruned_tracks", "pruned_albums", "pruned_artists")):
                logger.info(
                    "Pruned %d tracks / %d albums / %d artists no longer in Navidrome",
                    stats["pruned_tracks"], stats["pruned_albums"], stats["pruned_artists"],
                )

            self.last_sync = datetime.utcnow()
            self.last_stats = stats
            return stats

        except Exception as e:
            db.rollback()
            logger.error(f"Full sync failed: {e}")
            raise
        finally:
            self.is_syncing = False

    async def incremental_sync(self, db: Session) -> dict:
        """Incremental sync: only recently added albums/tracks."""
        self.is_syncing = True
        stats = {"albums": 0, "tracks": 0}

        try:
            logger.info("Starting incremental sync...")

            # Get recently added albums
            recent_albums = await self.client.get_album_list2(type_="newest", size=100)
            for album_data in recent_albums:
                album_id = album_data["id"]
                # Check if we already have this album
                existing = db.query(Album).filter(Album.id == album_id).first()
                if existing:
                    # Update metadata
                    existing.name = album_data.get("name", existing.name)
                    existing.play_count = album_data.get("playCount", existing.play_count)
                    existing.rating = album_data.get("rating", existing.rating)
                    existing.last_synced = datetime.utcnow()
                    stats["albums"] += 1
                    continue

                # New album — fetch full details
                try:
                    album_detail = await self.client.get_album(album_id)
                    album = Album(
                        id=album_data["id"],
                        name=album_data.get("name", "Unknown"),
                        artist_id=album_data.get("artistId"),
                        artist_name=album_data.get("artist"),
                        year=album_data.get("year"),
                        genre=album_data.get("genre"),
                        cover_art=album_data.get("coverArt"),
                        song_count=album_data.get("songCount", 0),
                        play_count=album_data.get("playCount", 0),
                        rating=album_data.get("rating", 0),
                        created_at=_parse_datetime(album_data.get("created")),
                        last_synced=datetime.utcnow(),
                    )
                    db.merge(album)
                    stats["albums"] += 1

                    # Sync tracks for this album
                    songs = album_detail.get("album", {}).get("song", [])
                    inner_album = album_detail.get("album", {})
                    for song_data in songs:
                        db.merge(_build_track(song_data, inner_album))
                        stats["tracks"] += 1
                except Exception as e:
                    logger.warning(f"Failed to sync new album {album_id}: {e}")
                    continue

            # Update play counts and ratings for existing tracks
            await self._update_play_counts(db)

            db.commit()
            self.last_sync = datetime.utcnow()
            logger.info(f"Incremental sync: {stats['albums']} albums, {stats['tracks']} tracks")
            return stats

        except Exception as e:
            db.rollback()
            logger.error(f"Incremental sync failed: {e}")
            raise
        finally:
            self.is_syncing = False

    async def _update_play_counts(self, db: Session):
        """Refresh play counts and ratings from recent albums."""
        try:
            recent = await self.client.get_album_list2(type_="recent", size=50)
            for album_data in recent:
                album_id = album_data["id"]
                try:
                    album_detail = await self.client.get_album(album_id)
                    songs = album_detail.get("album", {}).get("song", [])
                    for song_data in songs:
                        track = db.query(Track).filter(Track.id == song_data["id"]).first()
                        if track:
                            track.play_count = song_data.get("playCount", track.play_count)
                            track.rating = song_data.get("rating", track.rating)
                            track.starred = _parse_starred(song_data.get("starred"))
                            track.last_synced = datetime.utcnow()
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Play count update failed: {e}")


def build_search_key(artist, title) -> str:
    """Normalized "<artist>|<title>" key for MBID-free track resolution.

    ListenBrainz listens always carry artist_name + track_name, so this key is
    what resolves the ~50% of the library that has no musicBrainzId. Applied
    identically at sync time and at query time — the two must never diverge.

    Strips accents (NFKD -> ASCII), lowercases, and drops every non-alphanumeric
    character so spacing/hyphen/slash differences do not break the match:
        ("AC/DC", "Back In Black") -> "acdc|backinblack"
        ("Röyksopp", "Some Resolve") -> "royksopp|someresolve"
    """
    return f"{_normalize_text(artist)}|{_normalize_text(title)}"


def _normalize_text(value) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _build_track(song_data: dict, album: dict) -> Track:
    """Build a Track from a Subsonic song dict.

    Single construction site for both full and incremental sync so the MBID /
    search_key logic cannot drift between the two paths.

    `album` is the inner album dict from a getAlbum response
    ({"album": {...}}), used as a fallback for album_name.
    """
    title = song_data.get("title")
    artist = song_data.get("artist")
    mbid = (song_data.get("musicBrainzId") or "").strip() or None
    return Track(
        id=song_data["id"],
        title=title or "Unknown",
        album_id=song_data.get("parent"),
        album_name=song_data.get("album", album.get("name", "")),
        artist_id=song_data.get("artistId"),
        artist_name=artist,
        genre=song_data.get("genre"),
        year=song_data.get("year"),
        duration=song_data.get("duration"),
        track_number=song_data.get("track"),
        disc_number=song_data.get("discNumber"),
        play_count=song_data.get("playCount", 0),
        rating=song_data.get("rating", 0),
        starred=_parse_starred(song_data.get("starred")),
        created_at=_parse_datetime(song_data.get("created")),
        last_synced=datetime.utcnow(),
        recording_mbid=mbid,
        search_key=build_search_key(artist, title),
    )


def _prune(db: Session, model, seen_ids: set) -> int:
    """Delete rows of `model` whose id was not returned by Navidrome this run.

    A full sync is an authoritative snapshot, so vanished rows must go. When
    Navidrome re-scans a library it can reassign song IDs; without this, the old
    rows linger forever. That is not cosmetic — they land in `tracks.search_key`,
    so the ListenBrainz resolver can match a dead ID, and duplicate keys make it
    ambiguous which copy is live.

    Refuses to run on an empty `seen_ids`: an empty or failed fetch must never
    wipe the local library.
    """
    if not seen_ids:
        logger.warning("Prune skipped for %s: nothing seen this run", model.__name__)
        return 0

    existing = [row[0] for row in db.query(model.id).all()]
    stale = [row_id for row_id in existing if row_id not in seen_ids]
    if not stale:
        return 0

    deleted = 0
    # Batch the IN clause: SQLite caps bound parameters well below library size.
    for start in range(0, len(stale), 500):
        batch = stale[start:start + 500]
        deleted += db.query(model).filter(model.id.in_(batch)).delete(
            synchronize_session=False
        )
    db.commit()
    logger.info("Pruned %d stale %s row(s)", deleted, model.__name__)
    return deleted


def _parse_datetime(value) -> datetime | None:
    """Parse Navidrome datetime string to Python datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    # Navidrome returns ISO format like "2024-01-15T10:30:00Z"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_starred(value) -> bool:
    """Navidrome returns datetime string for starred items, False otherwise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value:
        return True
    return False
