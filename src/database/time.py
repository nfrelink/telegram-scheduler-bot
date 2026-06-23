"""Time helpers for SQLite storage.

SQLite stores timestamps as TEXT by convention. We store all timestamps in UTC in the
format used by SQLite's CURRENT_TIMESTAMP: "YYYY-MM-DD HH:MM:SS".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_sqlite_timestamp(dt: datetime) -> str:
    """Convert datetime to a UTC timestamp string suitable for SQLite comparisons."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    # SQLite CURRENT_TIMESTAMP has second precision and no timezone suffix.
    dt = dt.replace(microsecond=0, tzinfo=None)
    return dt.strftime(SQLITE_TIMESTAMP_FORMAT)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a SQLite timestamp string or datetime into a UTC-aware datetime.

    Returns None for null/missing values or unparseable strings.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
