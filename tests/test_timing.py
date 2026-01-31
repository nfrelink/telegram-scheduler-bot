from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scheduler.timing import calculate_next_run, parse_time_string, validate_schedule_pattern


def test_parse_time_string_valid() -> None:
    assert parse_time_string("09:00") == (9, 0)
    assert parse_time_string("23:59") == (23, 59)
    assert parse_time_string("0:5") == (0, 5)


def test_parse_time_string_invalid() -> None:
    assert parse_time_string("") is None
    assert parse_time_string("abc") is None
    assert parse_time_string("24:00") is None
    assert parse_time_string("12:60") is None


def test_validate_schedule_pattern_interval_requires_positive() -> None:
    ok, _ = validate_schedule_pattern({"type": "interval"})
    assert ok is False

    ok, _ = validate_schedule_pattern({"type": "interval", "hours": 1})
    assert ok is True

    ok, _ = validate_schedule_pattern({"type": "interval", "minutes": 30})
    assert ok is True


def test_calculate_next_run_interval() -> None:
    schedule = {"pattern": {"type": "interval", "hours": 2}, "timezone": "UTC"}
    after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after) == datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)


def test_calculate_next_run_daily_today_future() -> None:
    schedule = {"pattern": {"type": "daily", "times": ["09:00", "16:00"]}, "timezone": "UTC"}
    after = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after) == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


def test_calculate_next_run_daily_rollover() -> None:
    schedule = {"pattern": {"type": "daily", "times": ["09:00", "16:00"]}, "timezone": "UTC"}
    after = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after) == datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)


def test_calculate_next_run_weekly() -> None:
    schedule = {
        "pattern": {"type": "weekly", "days": ["monday", "wednesday"], "times": ["12:00"]},
        "timezone": "UTC",
    }
    # Monday 11:00 -> Monday 12:00
    after = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)  # Monday
    assert calculate_next_run(schedule, after=after) == datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    # Monday 12:00 (exact) -> Wednesday 12:00 (strictly after)
    after2 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after2) == datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)


def test_validate_custom_is_rejected() -> None:
    ok, _ = validate_schedule_pattern({"type": "custom", "cron": "0 */2 * * *"})
    assert ok is False


def test_calculate_next_run_rejects_custom() -> None:
    schedule = {"pattern": {"type": "custom", "cron": "0 */2 * * *"}, "timezone": "UTC"}
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _ = calculate_next_run(schedule, after=after)

