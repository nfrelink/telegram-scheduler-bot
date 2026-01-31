from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio


def pytest_configure() -> None:
    """Ensure src/ is importable in tests."""
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "src"
    sys.path.insert(0, str(src))


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set DATABASE_PATH to a temporary sqlite file for this test."""
    db_path = tmp_path / "scheduler_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DEFAULT_TIMEZONE", os.getenv("DEFAULT_TIMEZONE", "UTC") or "UTC")
    return db_path


@pytest_asyncio.fixture
async def initialized_db(db_env: Path) -> Path:
    """Initialize schema in a fresh temp database."""
    from database import init_database

    await init_database()
    return db_env

