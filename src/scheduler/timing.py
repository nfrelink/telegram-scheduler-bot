"""Schedule pattern validation and next-run calculations.

All returned datetimes are timezone-aware and in UTC.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


WEEKDAY_NAME_TO_INT: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _get_timezone(tz_name: str | None) -> tzinfo:
    if not tz_name:
        tz_name = os.getenv("DEFAULT_TIMEZONE", "UTC")

    # Always support UTC even if system tzdata is missing.
    if str(tz_name).upper() in {"UTC", "ETC/UTC"}:
        return timezone.utc

    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Timezone %r not found (missing tzdata?); falling back to UTC",
            tz_name,
        )
        return timezone.utc
    except Exception:
        logger.warning("Unknown timezone %r; falling back to UTC", tz_name)
        return timezone.utc


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_time_string(value: str) -> tuple[int, int] | None:
    """Parse HH:MM into (hour, minute)."""
    try:
        hour_str, minute_str = value.strip().split(":")
        hour = int(hour_str)
        minute = int(minute_str)
    except Exception:
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def validate_schedule_pattern(pattern: dict) -> tuple[bool, str]:
    """Validate schedule pattern is well-formed."""
    schedule_type = pattern.get("type")

    if schedule_type == "interval":
        hours = int(pattern.get("hours", 0) or 0)
        minutes = int(pattern.get("minutes", 0) or 0)
        if hours <= 0 and minutes <= 0:
            return False, "Interval schedule must include hours and/or minutes greater than 0."
        return True, "OK"

    if schedule_type == "daily":
        times = pattern.get("times", [])
        if not isinstance(times, list) or not times:
            return False, "Daily schedule must include a non-empty list of times."
        if not all(isinstance(t, str) and parse_time_string(t) for t in times):
            return False, "Daily times must be in HH:MM format."
        return True, "OK"

    if schedule_type == "weekly":
        days = pattern.get("days", [])
        times = pattern.get("times", [])
        if not isinstance(days, list) or not days:
            return False, "Weekly schedule must include a non-empty list of days."
        if not isinstance(times, list) or not times:
            return False, "Weekly schedule must include a non-empty list of times."
        if not all(isinstance(d, str) and d.lower() in WEEKDAY_NAME_TO_INT for d in days):
            return False, "Weekly days must be weekday names (e.g., monday, tuesday)."
        if not all(isinstance(t, str) and parse_time_string(t) for t in times):
            return False, "Weekly times must be in HH:MM format."
        return True, "OK"

    return False, "Unknown schedule type. Supported types: interval, daily, weekly."


def calculate_next_run(
    schedule: dict,
    *,
    after: datetime | None = None,
) -> datetime:
    """Calculate when a schedule should run next.

    Args:
        schedule: Schedule dict including keys: pattern, timezone (optional).
        after: Base time. If omitted, uses current UTC time.

    Returns:
        Next run time (UTC, timezone-aware).

    Raises:
        ValueError: If the schedule pattern is invalid.
    """
    if after is None:
        after = datetime.now(timezone.utc)
    after_utc = _ensure_aware_utc(after)

    pattern = schedule.get("pattern") or {}
    ok, reason = validate_schedule_pattern(pattern)
    if not ok:
        raise ValueError(reason)

    tz = _get_timezone(schedule.get("timezone"))

    match pattern.get("type"):
        case "interval":
            hours = int(pattern.get("hours", 0) or 0)
            minutes = int(pattern.get("minutes", 0) or 0)
            delta = timedelta(hours=hours, minutes=minutes)
            if delta.total_seconds() <= 0:
                raise ValueError("Interval must be greater than 0.")
            return after_utc + delta

        case "daily":
            return _next_daily_occurrence(after_utc, pattern["times"], tz)

        case "weekly":
            return _next_weekly_occurrence(after_utc, pattern["days"], pattern["times"], tz)

        case _:
            raise ValueError("Unknown schedule type.")


def _next_daily_occurrence(after_utc: datetime, times: list[str], tz: tzinfo) -> datetime:
    after_local = after_utc.astimezone(tz)
    parsed_times = sorted({parse_time_string(t) for t in times if parse_time_string(t)})
    if not parsed_times:
        raise ValueError("Daily schedule has no valid times.")

    # Check remaining times today.
    for hour, minute in parsed_times:
        candidate_local = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate_local > after_local:
            return candidate_local.astimezone(timezone.utc)

    # Otherwise, earliest time tomorrow.
    next_day = (after_local + timedelta(days=1)).date()
    hour, minute = parsed_times[0]
    candidate_local = datetime(next_day.year, next_day.month, next_day.day, hour, minute, tzinfo=tz)
    return candidate_local.astimezone(timezone.utc)


def _next_weekly_occurrence(
    after_utc: datetime,
    days: list[str],
    times: list[str],
    tz: tzinfo,
) -> datetime:
    after_local = after_utc.astimezone(tz)
    day_set = {WEEKDAY_NAME_TO_INT[d.lower()] for d in days}

    parsed_times = sorted({parse_time_string(t) for t in times if parse_time_string(t)})
    if not parsed_times:
        raise ValueError("Weekly schedule has no valid times.")

    # Search up to 14 days ahead to handle sparse weekly patterns.
    for offset in range(0, 14):
        candidate_date = (after_local + timedelta(days=offset)).date()
        if candidate_date.weekday() not in day_set:
            continue

        for hour, minute in parsed_times:
            candidate_local = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
                tzinfo=tz,
            )
            if candidate_local > after_local:
                return candidate_local.astimezone(timezone.utc)

    raise ValueError("Could not compute next weekly occurrence.")
