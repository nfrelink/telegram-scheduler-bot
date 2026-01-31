#!/usr/bin/env python3
"""Initialize the SQLite database and verify expected tables exist.

This is a smoke test you can run locally or inside Docker.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _add_src_to_path() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "src"
    sys.path.insert(0, str(src))


async def _verify() -> None:
    from database import init_database
    from database.connection import get_db, get_database_path

    await init_database()

    expected_tables = {
        "users",
        "channels",
        "schedules",
        "queued_posts",
        "verification_codes",
        "delivery_stats_daily",
        "user_context",
        "forward_origin_allowlist",
    }

    async with get_db() as db:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        rows = await cursor.fetchall()
        present = {r[0] for r in rows}  # type: ignore[index]

    missing = expected_tables - present
    if missing:
        raise RuntimeError(f"Missing expected tables: {sorted(missing)}")

    db_path = get_database_path()
    print("OK: database initialized and tables verified.")
    print(f"DATABASE_PATH={os.getenv('DATABASE_PATH', 'data/scheduler.db')}")
    print(f"Resolved DB path: {db_path}")


def main() -> None:
    _add_src_to_path()
    asyncio.run(_verify())


if __name__ == "__main__":
    main()

