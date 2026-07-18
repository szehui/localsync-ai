"""Library sync service — pulls metadata from Navidrome into local SQLite cache."""
import json
import logging
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

    async def full_sync(self, db: Session) -> dict:
        """Full library sync: artists → albums → tracks with metadata."""
        self.is_syncing = True
        stats = {"artists": 0, "albums": 0, "tracks": 0}

        try:
            logger.info("Starting full library sync...")

            # 1. Sync artists
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
                stats["artists"] += 1
            db.commit()
            logger.info(f"Synced {stats['artists']} artists")

            # 2. Sync albums (alphabetical by name, up to 1000)
            albums = await self.client.get_album_list2(type_="alphabeticalByName", size=1000)
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
                stats["albums"] += 1
            db.commit()
            logger.info(f"Synced {stats['albums']} albums")

            # 3. Sync tracks — iterate albums to get songs
            album_ids = [a.id for a in db.query(Album.id).all()]
            for album_id in album_ids:
                try:
                    album_detail = await self.client.get_album(album_id)
                    songs = album_detail.get("album", {}).get("song", [])
                    for song_data in songs:
                        track = Track(
                            id=song_data["id"],
                            title=song_data.get("title", "Unknown"),
                            album_id=song_data.get("parent"),
                            album_name=song_data.get("album", album_detail.get("album", {}).get("name", "")),
                            artist_id=song_data.get("artistId"),
                            artist_name=song_data.get("artist"),
                            genre=song_data.get("genre"),
                            year=song_data.get("year"),
                            duration=song_data.get("duration"),
                            track_number=song_data.get("track"),
                            disc_number=song_data.get("discNumber"),
                            play_count=song_data.get("playCount", 0),
                            rating=song_data.get("rating", 0),
                            starred=song_data.get("starred", False),
                            created_at=_parse_datetime(song_data.get("created")),
                            last_synced=datetime.utcnow(),
                        )
                        db.merge(track)
                        stats["tracks"] += 1
                except Exception as e:
                    logger.warning(f"Failed to sync album {album_id}: {e}")
                    continue
            db.commit()
            logger.info(f"Synced {stats['tracks']} tracks")

            self.last_sync = datetime.utcnow()
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
                    for song_data in songs:
                        track = Track(
                            id=song_data["id"],
                            title=song_data.get("title", "Unknown"),
                            album_id=song_data.get("parent"),
                            album_name=song_data.get("album", album_detail.get("album", {}).get("name", "")),
                            artist_id=song_data.get("artistId"),
                            artist_name=song_data.get("artist"),
                            genre=song_data.get("genre"),
                            year=song_data.get("year"),
                            duration=song_data.get("duration"),
                            track_number=song_data.get("track"),
                            disc_number=song_data.get("discNumber"),
                            play_count=song_data.get("playCount", 0),
                            rating=song_data.get("rating", 0),
                            starred=song_data.get("starred", False),
                            created_at=_parse_datetime(song_data.get("created")),
                            last_synced=datetime.utcnow(),
                        )
                        db.merge(track)
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
                            track.starred = song_data.get("starred", track.starred)
                            track.last_synced = datetime.utcnow()
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Play count update failed: {e}")


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
