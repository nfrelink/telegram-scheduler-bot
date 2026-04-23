from __future__ import annotations

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_init_database_migrates_legacy_users_table_without_data_loss(
    db_env,
) -> None:
    """Simulate an older DB missing users.timezone and ensure init_database upgrades it."""
    # Create a minimal legacy schema: users table without timezone column.
    async with aiosqlite.connect(db_env) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await conn.execute(
            "INSERT INTO users (id, username, first_name, last_name, is_admin) VALUES (?, ?, ?, ?, ?)",
            (123, "u", "f", "l", 0),
        )
        await conn.commit()

    from database import init_database

    await init_database()

    # Ensure the row is still present and the new column exists.
    async with aiosqlite.connect(db_env) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in await cur.fetchall()]
        assert "timezone" in cols

        cur2 = await conn.execute(
            "SELECT id, username, timezone FROM users WHERE id = 123"
        )
        row = await cur2.fetchone()
        assert row is not None
        assert int(row["id"]) == 123
        assert row["username"] == "u"
        assert row["timezone"] is None


@pytest.mark.asyncio
async def test_init_database_creates_media_fingerprints_and_duplicate_columns(
    db_env,
) -> None:
    """Ensure the media_fingerprints table and duplicate detection columns exist after init."""
    from database import init_database

    await init_database()

    async with aiosqlite.connect(db_env) as conn:
        conn.row_factory = aiosqlite.Row

        # media_fingerprints table exists
        cur = await conn.execute("PRAGMA table_info(media_fingerprints)")
        fp_cols = {r[1] for r in await cur.fetchall()}
        for expected in (
            "id",
            "channel_id",
            "file_unique_id",
            "dhash",
            "file_id",
            "media_type",
            "queued_post_id",
            "posted_at",
            "created_at",
        ):
            assert expected in fp_cols, f"Missing column: {expected}"

        # channels.duplicate_detection_enabled
        cur = await conn.execute("PRAGMA table_info(channels)")
        ch_cols = {r[1] for r in await cur.fetchall()}
        assert "duplicate_detection_enabled" in ch_cols

        # users.duplicate_alerts_enabled
        cur = await conn.execute("PRAGMA table_info(users)")
        u_cols = {r[1] for r in await cur.fetchall()}
        assert "duplicate_alerts_enabled" in u_cols


@pytest.mark.asyncio
async def test_init_database_migrates_legacy_schedules_to_next_planned_run_at(
    db_env,
) -> None:
    """Simulate a pre-Phase-1.1 schedules table missing next_planned_run_at.

    Regression test: SCHEMA_SQL must not reference next_planned_run_at in any
    statement that runs before _apply_migrations(), or upgrades crash with
    "no such column: next_planned_run_at" against any existing schedules row.
    """
    async with aiosqlite.connect(db_env) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL UNIQUE,
                channel_name TEXT NOT NULL,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                pattern TEXT NOT NULL,
                timezone TEXT DEFAULT 'UTC',
                state TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_run_at TIMESTAMP
            );
            """
        )
        await conn.execute(
            "INSERT INTO channels (user_id, channel_id, channel_name) VALUES (1, '-100', 'ch')"
        )
        await conn.execute(
            "INSERT INTO schedules (channel_id, name, pattern) VALUES (1, 'n', '{}')"
        )
        await conn.commit()

    from database import init_database

    await init_database()

    async with aiosqlite.connect(db_env) as conn:
        conn.row_factory = aiosqlite.Row

        cur = await conn.execute("PRAGMA table_info(schedules)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "next_planned_run_at" in cols

        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?",
            ("idx_schedules_next_planned_run_at",),
        )
        assert await cur.fetchone() is not None

        cur = await conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?",
            ("20260420_add_schedules_next_planned_run_at",),
        )
        assert await cur.fetchone() is not None

        cur = await conn.execute(
            "SELECT id, next_planned_run_at FROM schedules WHERE channel_id = 1"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_migration_adds_duplicate_columns_to_existing_schema(db_env) -> None:
    """Simulate a DB with existing tables but missing duplicate detection columns."""
    async with aiosqlite.connect(db_env) as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                timezone TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL UNIQUE,
                channel_name TEXT NOT NULL,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
            """
        )
        await conn.execute("INSERT INTO users (id, username) VALUES (1, 'test')")
        await conn.execute(
            "INSERT INTO channels (user_id, channel_id, channel_name) VALUES (1, '-100', 'ch')"
        )
        await conn.commit()

    from database import init_database

    await init_database()

    async with aiosqlite.connect(db_env) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("PRAGMA table_info(channels)")
        ch_cols = {r[1] for r in await cur.fetchall()}
        assert "duplicate_detection_enabled" in ch_cols

        cur = await conn.execute("PRAGMA table_info(users)")
        u_cols = {r[1] for r in await cur.fetchall()}
        assert "duplicate_alerts_enabled" in u_cols
