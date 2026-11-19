import logging
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Artist(Base):
    __tablename__ = "artists"
    id = Column(String, primary_key=True)  # Navidrome artist ID
    name = Column(String, nullable=False)
    album_count = Column(Integer, default=0)
    cover_art = Column(String, nullable=True)
    last_synced = Column(DateTime, default=datetime.utcnow)


class Album(Base):
    __tablename__ = "albums"
    id = Column(String, primary_key=True)  # Navidrome album ID
    name = Column(String, nullable=False)
    artist_id = Column(String, nullable=True)
    artist_name = Column(String, nullable=True)
    year = Column(Integer, nullable=True)
    genre = Column(String, nullable=True)
    cover_art = Column(String, nullable=True)
    song_count = Column(Integer, default=0)
    play_count = Column(Integer, default=0)
    rating = Column(Integer, default=0)
    created_at = Column(DateTime, nullable=True)  # When added to Navidrome
    last_synced = Column(DateTime, default=datetime.utcnow)


class Track(Base):
    __tablename__ = "tracks"
    id = Column(String, primary_key=True)  # Navidrome song ID
    title = Column(String, nullable=False)
    album_id = Column(String, nullable=True)
    album_name = Column(String, nullable=True)
    artist_id = Column(String, nullable=True)
    artist_name = Column(String, nullable=True)
    genre = Column(String, nullable=True)
    year = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)  # seconds
    track_number = Column(Integer, nullable=True)
    disc_number = Column(Integer, nullable=True)
    play_count = Column(Integer, default=0)
    rating = Column(Integer, default=0)  # 0-5 star rating
    starred = Column(Boolean, default=False)
    created_at = Column(DateTime, nullable=True)
    last_synced = Column(DateTime, default=datetime.utcnow)
    # MusicBrainz recording ID from Navidrome's Subsonic musicBrainzId field.
    # NULL when the album wasn't tagged (tagging in Sze's library is per-album).
    recording_mbid = Column(String, nullable=True, index=True)
    # Normalized "<artist>|<title>" for MBID-free resolution of ListenBrainz
    # listens, which always carry artist_name + track_name.
    search_key = Column(String, nullable=True, index=True)


class GeneratedPlaylist(Base):
    __tablename__ = "generated_playlists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    navidrome_playlist_id = Column(String, nullable=True)  # ID after pushing to Navidrome
    seed_track_id = Column(String, nullable=True)
    seed_track_name = Column(String, nullable=True)
    seed_playlist_id = Column(String, nullable=True)  # Navidrome playlist ID if generated from a playlist
    seed_playlist_name = Column(String, nullable=True)  # Navidrome playlist name if from a playlist
    strictness = Column(Integer, default=3)  # 1-5 slider
    track_count = Column(Integer, default=20)
    track_ids = Column(Text, nullable=True)  # JSON array of track IDs
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ConnectionConfig(Base):
    __tablename__ = "connection_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    url = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)


class SmartTrigger(Base):
    __tablename__ = "smart_triggers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    trigger_type = Column(String, nullable=False)  # "recency" | "heavy_rotation" | "scheduled"
    enabled = Column(Boolean, default=True)
    cron_expression = Column(String, nullable=True)  # For scheduled triggers
    threshold = Column(Integer, nullable=True)  # For heavy_rotation: play count threshold
    playlist_name = Column(String, nullable=True)  # Target playlist name
    navidrome_playlist_id = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class LastfmConfig(Base):
    __tablename__ = "lastfm_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    api_key = Column(String, nullable=False)
    username = Column(String, nullable=False)


class ListenbrainzConfig(Base):
    """Singleton table for ListenBrainz credentials.

    Primary listening-history source. Last.fm (LastfmConfig) remains as a
    fallback, consulted only when ListenBrainz yields nothing.
    """
    __tablename__ = "listenbrainz_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    user_token = Column(String, nullable=False)
    username = Column(String, nullable=False)
    api_base = Column(String, default="https://api.listenbrainz.org")
    labs_api_base = Column(String, default="https://labs.api.listenbrainz.org")
    enabled = Column(Boolean, default=True)
    last_connect_check = Column(DateTime, nullable=True)


class LbRecommendationCache(Base):
    """Cached user-level collaborative-filtering recommendations.

    The CF endpoint is expensive (1 call, 30/window budget) and only changes on
    a weekly cadence upstream, so it is refetched on a 24h TTL.
    """
    __tablename__ = "lb_recommendation_cache"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    mbids = Column(Text, nullable=False)  # JSON [{recording_mbid, score, latest_listened_at}]
    fetched_at = Column(DateTime, default=datetime.utcnow)


class LbSimilarCache(Base):
    """Cached per-recording Labs similar-recordings responses.

    The Labs endpoint accepts exactly one recording MBID per call, so with ~20
    seeds per run this cache is what keeps the call count sane. 7d TTL.
    """
    __tablename__ = "lb_similar_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_mbid = Column(String, nullable=False, index=True)
    payload = Column(Text, nullable=False)  # JSON list
    fetched_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrate existing tables: add columns that may not exist yet
    import sqlalchemy as sa
    inspector = sa.inspect(engine)
    columns = [c["name"] for c in inspector.get_columns("generated_playlists")]
    if "seed_playlist_id" not in columns:
        with engine.connect() as conn:
            conn.execute(sa.text("ALTER TABLE generated_playlists ADD COLUMN seed_playlist_id VARCHAR"))
            conn.execute(sa.text("ALTER TABLE generated_playlists ADD COLUMN seed_playlist_name VARCHAR"))
            conn.commit()

    # ListenBrainz support: recording MBID + normalized search key on tracks.
    track_columns = [c["name"] for c in inspector.get_columns("tracks")]
    added = []
    with engine.connect() as conn:
        if "recording_mbid" not in track_columns:
            conn.execute(sa.text("ALTER TABLE tracks ADD COLUMN recording_mbid VARCHAR"))
            added.append("recording_mbid")
        if "search_key" not in track_columns:
            conn.execute(sa.text("ALTER TABLE tracks ADD COLUMN search_key VARCHAR"))
            added.append("search_key")
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tracks_recording_mbid ON tracks (recording_mbid)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tracks_search_key ON tracks (search_key)"
        ))
        conn.commit()

    # Backfill search_key from existing rows (no Navidrome calls needed).
    # recording_mbid CANNOT be backfilled — it requires a library resync.
    if "search_key" in added:
        _backfill_search_keys()
        logger.info(
            "tracks.search_key backfilled; tracks.recording_mbid needs a full library resync"
        )


def _backfill_search_keys():
    """Populate tracks.search_key for rows synced before the column existed."""
    import sqlalchemy as sa

    from app.services.sync import build_search_key

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, artist_name, title FROM tracks WHERE search_key IS NULL")
        ).fetchall()
        for track_id, artist_name, title in rows:
            conn.execute(
                sa.text("UPDATE tracks SET search_key = :k WHERE id = :i"),
                {"k": build_search_key(artist_name, title), "i": track_id},
            )
        conn.commit()