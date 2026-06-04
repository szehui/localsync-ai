from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import settings

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


class GeneratedPlaylist(Base):
    __tablename__ = "generated_playlists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    navidrome_playlist_id = Column(String, nullable=True)  # ID after pushing to Navidrome
    seed_track_id = Column(String, nullable=True)
    seed_track_name = Column(String, nullable=True)
    strictness = Column(Integer, default=3)  # 1-5 slider
    track_count = Column(Integer, default=20)
    track_ids = Column(Text, nullable=True)  # JSON array of track IDs
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
