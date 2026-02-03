from __future__ import annotations

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_init_database_migrates_legacy_users_table_without_data_loss(db_env) -> None:
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

        cur2 = await conn.execute("SELECT id, username, timezone FROM users WHERE id = 123")
        row = await cur2.fetchone()
        assert row is not None
        assert int(row["id"]) == 123
        assert row["username"] == "u"
        assert row["timezone"] is None

