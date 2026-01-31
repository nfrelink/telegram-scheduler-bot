"""Time helpers for SQLite storage.

SQLite stores timestamps as TEXT by convention. We store all timestamps in UTC in the
format used by SQLite's CURRENT_TIMESTAMP: "YYYY-MM-DD HH:MM:SS".
"""

from __future__ import annotations

from datetime import datetime, timezone

SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_sqlite_timestamp(dt: datetime) -> str:
    """Convert datetime to a UTC timestamp string suitable for SQLite comparisons."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # SQLite CURRENT_TIMESTAMP has second precision and no timezone suffix.
    dt = dt.replace(microsecond=0, tzinfo=None)
    return dt.strftime(SQLITE_TIMESTAMP_FORMAT)

