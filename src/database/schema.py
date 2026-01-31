"""SQLite schema and initialization."""

from __future__ import annotations

import logging

from .connection import get_db

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,  -- Telegram user ID
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_is_admin ON users(is_admin);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,  -- Telegram channel ID (e.g., @channelname or -100123456)
    channel_name TEXT NOT NULL,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_channels_user_id ON channels(user_id);
CREATE INDEX IF NOT EXISTS idx_channels_channel_id ON channels(channel_id);
CREATE INDEX IF NOT EXISTS idx_channels_is_active ON channels(is_active);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,  -- JSON: {"type": "daily", "times": ["09:00"]}
    timezone TEXT DEFAULT 'UTC',
    state TEXT DEFAULT 'active',  -- 'active', 'paused', 'empty_paused'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_run_at TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_schedules_channel_id ON schedules(channel_id);
CREATE INDEX IF NOT EXISTS idx_schedules_state ON schedules(state);
CREATE INDEX IF NOT EXISTS idx_schedules_last_run ON schedules(last_run_at);
CREATE INDEX IF NOT EXISTS idx_schedules_state_active ON schedules(state) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS queued_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    file_id TEXT,  -- Telegram file_id (preferred method)
    file_path TEXT,  -- Local file path (fallback)
    media_type TEXT NOT NULL,  -- 'photo', 'video', 'document', 'media_group'
    caption TEXT,
    caption_parse_mode TEXT,  -- NULL (plain), 'markdownv2', or 'html'
    caption_entities TEXT,  -- JSON list of Telegram MessageEntity dicts
    media_group_data TEXT,  -- JSON array for media groups
    position INTEGER NOT NULL,  -- Queue position (FIFO)
    retry_count INTEGER DEFAULT 0,
    scheduled_for TIMESTAMP,  -- For immediate/missed posts
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_queued_posts_schedule_id ON queued_posts(schedule_id);
CREATE INDEX IF NOT EXISTS idx_queued_posts_position ON queued_posts(schedule_id, position);
CREATE INDEX IF NOT EXISTS idx_queued_posts_scheduled_for ON queued_posts(scheduled_for) WHERE scheduled_for IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_queued_posts_retry ON queued_posts(retry_count);

CREATE TABLE IF NOT EXISTS verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,  -- Channel being verified (Telegram channel ID)
    code TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_verification_codes_code ON verification_codes(code);
CREATE INDEX IF NOT EXISTS idx_verification_codes_user_channel ON verification_codes(user_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_verification_codes_expires ON verification_codes(expires_at);

-- Per-user selection context (small; one row per user)
CREATE TABLE IF NOT EXISTS user_context (
    user_id INTEGER PRIMARY KEY,
    selected_channel_id INTEGER,
    selected_schedule_id INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (selected_channel_id) REFERENCES channels(id) ON DELETE SET NULL,
    FOREIGN KEY (selected_schedule_id) REFERENCES schedules(id) ON DELETE SET NULL
);

-- Aggregated daily delivery stats (small table; one row per day)
CREATE TABLE IF NOT EXISTS delivery_stats_daily (
    day TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' in UTC
    posts_sent INTEGER NOT NULL DEFAULT 0,
    send_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_database() -> None:
    """Initialize database schema (idempotent)."""
    async with get_db() as db:
        logger.info("Applying database schema...")
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        await _apply_migrations(db)
        logger.info("Database schema applied.")


async def _apply_migrations(db) -> None:  # type: ignore[no-untyped-def]
    """Apply lightweight, additive schema migrations."""
    await _ensure_column(db, table="queued_posts", column="caption_parse_mode", sql_type="TEXT")
    await _ensure_column(db, table="queued_posts", column="caption_entities", sql_type="TEXT")
    await db.commit()


async def _ensure_column(db, *, table: str, column: str, sql_type: str) -> None:  # type: ignore[no-untyped-def]
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {str(r[1]) for r in rows}  # type: ignore[index]
    if column in existing:
        return

    logger.info("Migrating DB: adding %s.%s", table, column)
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

