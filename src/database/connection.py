"""Database connection and transaction helpers."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite


def get_database_path() -> Path:
    """Get the SQLite DB path (ensuring parent directory exists)."""
    raw = os.getenv("DATABASE_PATH", "data/scheduler.db")
    path = Path(raw)
    if not path.is_absolute():
        # Interpret relative paths as relative to project working directory.
        path = Path.cwd() / path

    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a DB connection with foreign keys enabled."""
    db_path = get_database_path()
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            db.row_factory = aiosqlite.Row
            yield db
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e).lower():
            raise RuntimeError(
                "Unable to open the SQLite database file.\n\n"
                f"DATABASE_PATH resolves to: {db_path}\n\n"
                "If you are running via Docker with a bind mount like ./data:/app/data, "
                "the most common cause is permissions on the host ./data directory.\n\n"
                "Fix (example):\n"
                "- mkdir -p data\n"
                "- sudo chown -R 1000:1000 data\n"
                "- chmod 755 data\n"
            ) from e
        raise


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """Transaction context manager (commit on success, rollback on error)."""
    async with get_db() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

