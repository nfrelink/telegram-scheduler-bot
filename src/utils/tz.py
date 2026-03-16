"""Shared timezone utilities."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def default_timezone_name() -> str:
    return os.getenv("DEFAULT_TIMEZONE", "UTC") or "UTC"


def is_valid_timezone(tz_name: str) -> bool:
    if tz_name.upper() in {"UTC", "ETC/UTC"}:
        return True
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False
