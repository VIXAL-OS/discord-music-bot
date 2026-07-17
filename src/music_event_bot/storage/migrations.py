from __future__ import annotations

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            title_normalized TEXT NOT NULL,
            artist TEXT,
            venue TEXT,
            venue_normalized TEXT NOT NULL DEFAULT '',
            location TEXT,
            starts_at TEXT,
            ends_at TEXT,
            timezone TEXT,
            url TEXT,
            image_url TEXT,
            description TEXT,
            genres_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            match_reasons_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE event_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            source_name TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_url TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}',
            last_observed_at TEXT NOT NULL,
            UNIQUE(source_name, source_event_id)
        );

        CREATE TABLE reviews (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            review_channel_id TEXT,
            review_message_id TEXT,
            notified_at TEXT,
            decision TEXT,
            reviewer_id TEXT,
            reason TEXT,
            decided_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE publications (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            scheduled_event_id TEXT,
            announcement_message_id TEXT,
            state TEXT NOT NULL DEFAULT 'not_started',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE taste_preferences (
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(kind, value, source)
        );

        CREATE TABLE genre_roles (
            genre TEXT PRIMARY KEY,
            role_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX idx_events_status_start ON events(status, starts_at);
        CREATE INDEX idx_events_normalized ON events(title_normalized, venue_normalized, starts_at);
        CREATE INDEX idx_sources_event ON event_sources(event_id);
        CREATE INDEX idx_jobs_name_started ON job_runs(job_name, started_at DESC);
        """,
    ),
    (
        2,
        """
        ALTER TABLE events ADD COLUMN venue_latitude REAL;
        ALTER TABLE events ADD COLUMN venue_longitude REAL;
        CREATE INDEX idx_events_review_priority
            ON events(status, score DESC, starts_at, id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE artist_tag_fetches (
            artist_normalized TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'lastfm',
            fetched_at TEXT NOT NULL,
            tag_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(artist_normalized, source)
        );

        CREATE TABLE artist_tags (
            artist_normalized TEXT NOT NULL,
            tag TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'lastfm',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(artist_normalized, tag, source)
        );

        CREATE TABLE genre_tag_map (
            tag TEXT PRIMARY KEY,
            buckets_json TEXT NOT NULL DEFAULT '[]',
            broad_genres_json TEXT NOT NULL DEFAULT '[]',
            model TEXT NOT NULL DEFAULT '',
            mapped_at TEXT NOT NULL
        );

        CREATE INDEX idx_artist_tags_tag ON artist_tags(tag);
        """,
    ),
    (
        4,
        """
        ALTER TABLE events ADD COLUMN artists_json TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    (
        5,
        """
        ALTER TABLE reviews ADD COLUMN card_hash TEXT;
        """,
    ),
    (
        6,
        """
        CREATE TABLE rsvps (
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('going', 'interested', 'declined')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(event_id, user_id)
        );
        """,
    ),
    (
        7,
        """
        ALTER TABLE publications ADD COLUMN reminder_sent_at TEXT;
        """,
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
