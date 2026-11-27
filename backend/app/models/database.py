import logging
from sqlalchemy import create_engine, event, or_, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
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


if "sqlite" in settings.database_url:
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        # SQLite ignores FK constraints (and their ON DELETE CASCADE) unless
        # this pragma is set per-connection.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Artist(Base):
    __tablename__ = "artists"
    id = Column(String, primary_key=True)  # Navidrome artist ID
    name = Column(String, nullable=False)
    album_count = Column(Integer, default=0)
    cover_art = Column(String, nullable=True)
    last_synced = Column(DateTime, default=datetime.utcnow)


class User(Base):
    """One tenant. Credentials are per-user because each user may point at a
    different Navidrome instance; auth itself is still delegated to Navidrome,
    so the password here is only replayed when the backend talks upstream.

    (navidrome_url, username) is unique rather than username alone: the same
    username on two servers is two distinct tenants.
    """
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("navidrome_url", "username", name="uq_users_url_username"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    navidrome_url = Column(String, nullable=False)
    navidrome_password = Column(String, nullable=False)
    is_owner = Column(Boolean, default=False)  # first user; can manage others
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class TrackAnnotation(Base):
    """Per-user state layered over the shared library.

    Tracks are global (one Navidrome library row per song) but play counts,
    ratings, and stars are personal, so they live here keyed by
    (user, track) instead of on Track. The composite PK both enforces
    one-row-per-user-per-track and gives the natural lookup index.

    Rows vanish with either parent: a deleted user takes their annotations
    with them, and a track dropped from the library takes every user's
    annotation for it.
    """
    __tablename__ = "track_annotations"
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    track_id = Column(
        String,
        ForeignKey("tracks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    play_count = Column(Integer, default=0)
    rating = Column(Integer, default=0)  # 0-5 star rating
    starred = Column(Boolean, default=False)
    last_played_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


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
    # Last time Navidrome played this track ("played" in the Subsonic song
    # object). The time-window mixes (On Repeat / Rewind / Rediscover) are built
    # from this; it stays NULL until the library has been synced once after the
    # column was added.
    last_played_at = Column(DateTime, nullable=True)
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
    # NULL means created before multi-user support; the migration backfills
    # these to the owner user so no playlist becomes unreachable.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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
    """Legacy singleton config. Read only by the one-time per-user migration,
    then superseded by per-user tables; kept so that migration can run."""
    __tablename__ = "connection_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    url = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)


class SmartTrigger(Base):
    __tablename__ = "smart_triggers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # NULL on pre-multi-user rows; the migration backfills them to the owner.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    trigger_type = Column(String, nullable=False)  # "recency" | "heavy_rotation" | "scheduled" | "library_mix"
    enabled = Column(Boolean, default=True)
    cron_expression = Column(String, nullable=True)  # For scheduled triggers
    threshold = Column(Integer, nullable=True)  # For heavy_rotation: play count threshold
    # For library_mix: which local mix to build (deep_cuts | on_repeat | rewind
    # | rediscover | unplayed). See services/mixes.py MIX_SPECS.
    mix_kind = Column(String, nullable=True)
    playlist_name = Column(String, nullable=True)  # Target playlist name
    navidrome_playlist_id = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class LastfmConfig(Base):
    """Legacy singleton config. Read only by the one-time per-user migration,
    then superseded by LastfmUserConfig; kept so that migration can run."""
    __tablename__ = "lastfm_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    api_key = Column(String, nullable=False)
    username = Column(String, nullable=False)


class LidarrConfig(Base):
    """Singleton table to store Lidarr connection configuration."""
    __tablename__ = "lidarr_config"
    id = Column(Integer, primary_key=True, default=1)
    url = Column(String, nullable=False)
    api_key = Column(String, nullable=False)


class ListenbrainzConfig(Base):
    """Singleton table for ListenBrainz credentials.

    Primary listening-history source. Last.fm (LastfmConfig) remains as a
    fallback, consulted only when ListenBrainz yields nothing.

    Legacy singleton: read only by the one-time per-user migration, then
    superseded by ListenbrainzUserConfig.
    """
    __tablename__ = "listenbrainz_config"
    id = Column(Integer, primary_key=True, default=1)  # singleton row
    user_token = Column(String, nullable=False)
    username = Column(String, nullable=False)
    api_base = Column(String, default="https://api.listenbrainz.org")
    labs_api_base = Column(String, default="https://labs.api.listenbrainz.org")
    enabled = Column(Boolean, default=True)
    last_connect_check = Column(DateTime, nullable=True)


class ListenbrainzUserConfig(Base):
    """Per-user ListenBrainz credentials.

    ListenBrainz auth is separate from Navidrome auth, so a user who never
    linked a scrobbler has no token — that is a valid state, and this row may
    simply be absent. user_id is the primary key, so each user has at most one
    row, and deleting the user cascades the row away.
    """
    __tablename__ = "listenbrainz_user_config"
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_token = Column(String, nullable=False)
    username = Column(String, nullable=False)
    api_base = Column(String, default="https://api.listenbrainz.org")
    labs_api_base = Column(String, default="https://labs.api.listenbrainz.org")
    enabled = Column(Boolean, default=True)
    last_connect_check = Column(DateTime, nullable=True)


class LastfmUserConfig(Base):
    """Per-user Last.fm credentials.

    See ListenbrainzUserConfig: per-user, at most one row per user, and the
    row may be absent when the user never linked Last.fm. Deleting the user
    cascades the row away.
    """
    __tablename__ = "lastfm_user_config"
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    api_key = Column(String, nullable=False)
    username = Column(String, nullable=False)


class LbRecommendationCache(Base):
    """Cached per-user collaborative-filtering recommendations.

    CF recommendations are computed from ONE listener's history, so they are
    only meaningful for that listener: one row per user, and a deleted user
    takes their cache row with them. Sharing this between accounts would
    recommend one person's taste to another.

    The CF endpoint is expensive (1 call, 30/window budget) and only changes on
    a weekly cadence upstream, so it is refetched on a 24h TTL.

    Contrast LbSimilarCache, which is deliberately NOT per-user: neighbours of
    a recording are a property of the recording, not of who asked for them, so
    that cache is shared across all users.
    """
    __tablename__ = "lb_recommendation_cache"
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mbids = Column(Text, nullable=False)  # JSON [{recording_mbid, score, latest_listened_at}]
    fetched_at = Column(DateTime, default=datetime.utcnow)


class LbSimilarCache(Base):
    """Cached per-recording Labs similar-recordings responses.

    The Labs endpoint accepts exactly one recording MBID per call, so with ~20
    seeds per run this cache is what keeps the call count sane. 7d TTL.

    Intentionally global, not per-user: the similar recordings of an MBID are
    the same fact regardless of which user seeded the query. See
    LbRecommendationCache for the per-user cache.
    """
    __tablename__ = "lb_similar_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_mbid = Column(String, nullable=False, index=True)
    payload = Column(Text, nullable=False)  # JSON list
    fetched_at = Column(DateTime, default=datetime.utcnow)


class DiscoveryOffer(Base):
    """One candidate recording offered to a user in the discovery feed.

    Novelty triage bins each candidate against the local library:
      * already owned            -> never stored
      * artist present, album missing -> bucket "artist_known"  (gap-fill)
      * artist absent entirely        -> bucket "new_artist"    (true discovery)

    `status` is the dedupe memory: `offered` shows in the feed, `saved` means the
    album was pushed to Lidarr, `dismissed` never resurfaces. Without this the
    same ~50 candidates would be re-offered on every run.

    Offers are per-user, not household-global. The dedupe memory is one
    person's opinion ("I don't want this album"), so the uniqueness that makes
    it work is (user_id, recording_mbid): the same recording can be offered to
    several users, and one user's dismissal must not hide it from anyone else.
    Deleting a user takes their offers with them, since a feed nobody owns is
    unreachable.
    """
    __tablename__ = "discovery_offers"
    __table_args__ = (
        UniqueConstraint("user_id", "recording_mbid",
                         name="uq_discovery_offer_user_mbid"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recording_mbid = Column(String, nullable=False, index=True)
    artist_name = Column(String, nullable=True, index=True)
    track_title = Column(String, nullable=True)
    release_name = Column(String, nullable=True)
    release_mbid = Column(String, nullable=True)
    source = Column(String, nullable=False, default="labs")  # labs | cf
    score = Column(Float, default=0.0)
    evidence = Column(Text, nullable=True)  # JSON [{seed_artist, seed_title}]
    bucket = Column(String, nullable=False, default="new_artist")
    status = Column(String, nullable=False, default="offered", index=True)
    lidarr_album_id = Column(Integer, nullable=True)
    lidarr_artist_name = Column(String, nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


class DiscoveryState(Base):
    """Singleton row describing the last discovery refresh.

    The refresh is a background job (rate-limited upstream calls), so the UI
    polls this to know whether to show a spinner or the feed.
    """
    __tablename__ = "discovery_state"
    id = Column(Integer, primary_key=True, default=1)
    last_run = Column(DateTime, nullable=True)
    last_status = Column(String, nullable=True)  # ok | running | error: ...
    last_error = Column(Text, nullable=True)
    seeds_used = Column(Integer, default=0)
    candidates_seen = Column(Integer, default=0)
    offers_added = Column(Integer, default=0)
    calls_made = Column(Integer, default=0)


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
        if "last_played_at" not in track_columns:
            # Populated from Navidrome's `played` field on the next library
            # sync; the time-window mixes stay empty until then.
            conn.execute(sa.text("ALTER TABLE tracks ADD COLUMN last_played_at DATETIME"))
            added.append("last_played_at")
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tracks_recording_mbid ON tracks (recording_mbid)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tracks_search_key ON tracks (search_key)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tracks_last_played_at ON tracks (last_played_at)"
        ))
        conn.commit()

    # Library-mix triggers need to remember which local mix they build.
    trigger_columns = [c["name"] for c in inspector.get_columns("smart_triggers")]
    if "mix_kind" not in trigger_columns:
        with engine.connect() as conn:
            conn.execute(sa.text("ALTER TABLE smart_triggers ADD COLUMN mix_kind VARCHAR"))
            conn.commit()

    # Backfill search_key from existing rows (no Navidrome calls needed).
    # recording_mbid CANNOT be backfilled — it requires a library resync.
    if "search_key" in added:
        _backfill_search_keys()
        logger.info(
            "tracks.search_key backfilled; tracks.recording_mbid needs a full library resync"
        )

    # Multi-user ownership: `create_all()` does NOT alter existing tables, so an
    # upgraded database still lacks these columns even though the models declare
    # them. Without this the backfill below dies with
    # "no such column: owner_id" and the app never finishes starting.
    with engine.connect() as conn:
        for table in ("smart_triggers", "generated_playlists"):
            cols = [c["name"] for c in inspector.get_columns(table)]
            if "owner_id" not in cols:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER"))
            # ALTER TABLE cannot carry the model's index=True, so create it here.
            conn.execute(sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_owner_id ON {table} (owner_id)"
            ))
        conn.commit()

    # Per-user CF recommendation cache: the old table was a singleton (id=1)
    # and has no user_id, which create_all() will never add. There is no
    # defensible owner for that row — it was computed from whichever single
    # listener the pre-multi-user install had — so drop and recreate rather
    # than migrate. The cache has a 24h TTL, so the only cost is one extra API
    # call. The guard keeps a fresh DB (where create_all already built the
    # right table) untouched.
    if inspector.has_table("lb_recommendation_cache"):
        lb_cache_cols = [
            c["name"] for c in inspector.get_columns("lb_recommendation_cache")
        ]
        if "user_id" not in lb_cache_cols:
            logger.info(
                "Migrating lb_recommendation_cache to per-user: dropping "
                "singleton row (no owner, 24h TTL)"
            )
            with engine.connect() as conn:
                conn.execute(sa.text("DROP TABLE lb_recommendation_cache"))
                conn.commit()
            Base.metadata.tables["lb_recommendation_cache"].create(bind=engine)

    # One-time multi-user migration: adopt legacy singleton config into
    # per-user rows, then backfill owner_id on pre-multi-user triggers and
    # playlists. Safe to re-run on every startup.
    with SessionLocal() as db:
        _migrate_multi_user(db)

    # Per-user discovery offers. The old table carried `recording_mbid UNIQUE`,
    # which made it impossible for a second user to ever be offered a recording
    # the first user had already seen. SQLite cannot drop a UNIQUE constraint in
    # place, so the table is rebuilt and its rows copied across — deliberately
    # AFTER the multi-user adoption above, because legacy rows can only be
    # attributed once an owner user exists. Guarded so a fresh DB (already built
    # per-user by create_all) and a second startup are both no-ops.
    if inspector.has_table("discovery_offers"):
        offer_columns = [
            c["name"] for c in inspector.get_columns("discovery_offers")
        ]
        if "user_id" not in offer_columns:
            _rebuild_discovery_offers(offer_columns)


def _rebuild_discovery_offers(legacy_columns: list[str]) -> None:
    """Recreate discovery_offers with a per-user unique key, keeping its rows.

    SQLite has no ALTER TABLE ... DROP CONSTRAINT, so the only way to replace
    `recording_mbid UNIQUE` with `(user_id, recording_mbid)` is to rebuild. The
    rows are NOT disposable: `status` is the user's save/dismiss memory, and
    dropping it would re-offer every album they already rejected.

    Legacy rows predate multi-user support, so they all belong to the owner
    user — there was only ever one listener. If the owner flag is missing but
    exactly one user row exists (users created by plain login before the
    adoption step existed also carry is_owner=False), that user is still the
    sole listener, so the rows are theirs. With several candidate users there
    is nobody to attribute to, and guessing would hand one tenant another
    person's dismissal history, so the rebuild is skipped and the old table
    left intact.

    Column names come from the model rather than a hardcoded list, but only ones
    that actually exist in the old table are copied; `user_id` is the exception
    and is supplied as the owner id.
    """
    import sqlalchemy as sa

    table = Base.metadata.tables["discovery_offers"]
    existing = set(legacy_columns)
    copy_columns = [
        c.name for c in table.columns if c.name != "user_id" and c.name in existing
    ]

    with SessionLocal() as db:
        owner = db.query(User).filter_by(is_owner=True).first()
        if owner is None:
            users = db.query(User).limit(2).all()
            if len(users) == 1:
                owner = users[0]
        owner_id = owner.id if owner is not None else None

    if owner_id is None:
        logger.warning(
            "Discovery offers: no owner user exists to attribute legacy rows to "
            "— leaving discovery_offers on the pre-multi-user schema. The "
            "discovery feed will error until the table is rebuilt; set "
            "is_owner=True on the account that owns the old dismissed/saved "
            "offers and restart."
        )
        return

    copy_sql = ", ".join(copy_columns)
    insert_sql = (
        f"INSERT INTO discovery_offers (user_id, {copy_sql}) "
        f"SELECT :owner_id, {copy_sql} FROM discovery_offers_old"
    )

    with engine.connect() as conn:
        conn.execute(
            sa.text("ALTER TABLE discovery_offers RENAME TO discovery_offers_old")
        )
        # Renaming a table keeps its explicitly named indexes, and index names
        # are global in SQLite, so creating the new table would collide with
        # them. (sqlite_autoindex_* entries belong to the renamed table and are
        # left alone — they cannot be dropped, and do not collide.)
        stale_indexes = conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='discovery_offers_old' AND name NOT LIKE 'sqlite_%'"
        )).scalars().all()
        for name in stale_indexes:
            conn.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))

        table.create(bind=conn)
        result = conn.execute(sa.text(insert_sql), {"owner_id": owner_id})
        conn.execute(sa.text("DROP TABLE discovery_offers_old"))
        conn.commit()

    logger.info(
        "Discovery offers: rebuilt per-user, %s legacy rows attributed to owner %s",
        result.rowcount, owner_id,
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


def _migrate_multi_user(db) -> None:
    """Adopt legacy singleton rows into the users table. Idempotent.

    Reads the pre-multi-user singleton config (ConnectionConfig,
    ListenbrainzConfig, LastfmConfig) and the per-track state that still lives
    on Track, then copies it into per-user rows owned by a single owner user.
    Safe to re-run on every startup: if the users table is already populated
    the adoption block is skipped, and the owner_id backfill only touches rows
    whose owner_id is NULL, committing only when something changed.

    Legacy columns (tracks.play_count / rating / starred / last_played_at) and
    the legacy singleton tables are intentionally left in place — they are the
    rollback path.
    """
    if db.query(User).first() is None:
        legacy_conn = db.get(ConnectionConfig, 1)
        if legacy_conn is None:
            logger.info(
                "Multi-user migration: no legacy connection to adopt — skipping"
            )
        else:
            owner = User(
                username=legacy_conn.username,
                display_name=legacy_conn.username,
                navidrome_url=legacy_conn.url,
                navidrome_password=legacy_conn.password,
                is_owner=True,
            )
            db.add(owner)
            db.flush()  # populate owner.id before child rows reference it

            lb = db.get(ListenbrainzConfig, 1)
            if lb is not None:
                db.add(
                    ListenbrainzUserConfig(
                        user_id=owner.id,
                        user_token=lb.user_token,
                        username=lb.username,
                        api_base=lb.api_base,
                        labs_api_base=lb.labs_api_base,
                        enabled=lb.enabled,
                    )
                )

            lf = db.get(LastfmConfig, 1)
            if lf is not None:
                db.add(
                    LastfmUserConfig(
                        user_id=owner.id,
                        api_key=lf.api_key,
                        username=lf.username,
                    )
                )

            # All-zero track state is deliberately skipped: it is
            # indistinguishable from "no annotation" and would cost one insert
            # per track in the library.
            tracks = (
                db.query(Track)
                .filter(
                    or_(
                        Track.play_count > 0,
                        Track.starred.is_(True),
                        Track.last_played_at.isnot(None),
                        Track.rating > 0,
                    )
                )
                .all()
            )
            for t in tracks:
                db.add(
                    TrackAnnotation(
                        user_id=owner.id,
                        track_id=t.id,
                        play_count=t.play_count or 0,
                        rating=t.rating or 0,
                        starred=bool(t.starred),
                        last_played_at=t.last_played_at,
                    )
                )
            db.commit()
            logger.info(
                "Multi-user migration: adopted user %s; annotated %d tracks",
                legacy_conn.username,
                len(tracks),
            )

    # Runs on every startup: legacy triggers/playlists predate owner_id, so
    # without this they would be unreachable to every user.
    owner = db.query(User).filter_by(is_owner=True).first()
    if owner is None:
        return
    changed = 0
    for model in (SmartTrigger, GeneratedPlaylist):
        changed += (
            db.query(model)
            .filter(model.owner_id.is_(None))
            .update({model.owner_id: owner.id}, synchronize_session=False)
        ) or 0
    if changed:
        db.commit()
        logger.info("Multi-user migration: backfilled owner_id on %d rows", changed)
