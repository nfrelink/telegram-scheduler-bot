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


def test_calculate_next_run_daily_timezone_dst_shift_europe_amsterdam() -> None:
    """Daily schedules should stay anchored to local time across DST."""
    schedule = {"pattern": {"type": "daily", "times": ["09:00"]}, "timezone": "Europe/Amsterdam"}

    # Before EU DST starts (CET, UTC+1): 09:00 local == 08:00 UTC.
    after_winter = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after_winter) == datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)

    # Before EU DST ends (CEST, UTC+2): next day may be CET again.
    after_summer = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after_summer) == datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)


def test_calculate_next_run_daily_dst_gap_is_handled_europe_amsterdam() -> None:
    """Nonexistent local times (spring-forward gap) should schedule to the next valid instant."""
    schedule = {"pattern": {"type": "daily", "times": ["02:30"]}, "timezone": "Europe/Amsterdam"}

    # EU DST start day 2026-03-29: local time jumps 02:00 -> 03:00, so 02:30 doesn't exist.
    # The implementation returns a UTC instant that corresponds to ~03:30 local time.
    after = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
    assert calculate_next_run(schedule, after=after) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


def test_validate_custom_is_rejected() -> None:
    ok, _ = validate_schedule_pattern({"type": "custom", "cron": "0 */2 * * *"})
    assert ok is False


def test_calculate_next_run_rejects_custom() -> None:
    schedule = {"pattern": {"type": "custom", "cron": "0 */2 * * *"}, "timezone": "UTC"}
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _ = calculate_next_run(schedule, after=after)


# ---------------------------------------------------------------------------
# Branch coverage: _get_timezone, validate_schedule_pattern edges,
# calculate_next_run defaults and naive `after`.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from scheduler.timing import _get_timezone  # noqa: E402


def test_get_timezone_none_falls_back_to_env_or_utc(monkeypatch) -> None:
    """No name → consult DEFAULT_TIMEZONE; fall back to UTC if unset."""
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    assert _get_timezone(None) is timezone.utc


def test_get_timezone_explicit_utc_short_circuits() -> None:
    """The string 'UTC' avoids the ZoneInfo lookup so it works even without
    system tzdata installed."""
    assert _get_timezone("UTC") is timezone.utc
    assert _get_timezone("etc/utc") is timezone.utc


def test_get_timezone_unknown_zone_falls_back_to_utc(caplog) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="scheduler.timing"):
        tz = _get_timezone("Mars/Olympus_Mons")
    assert tz is timezone.utc
    assert any("not found" in rec.message or "Unknown timezone" in rec.message
               for rec in caplog.records)


def test_get_timezone_non_string_falls_back_to_utc(caplog) -> None:
    """Defensive: a non-string value triggers the generic Exception branch."""
    import logging
    with caplog.at_level(logging.WARNING, logger="scheduler.timing"):
        tz = _get_timezone(12345)  # type: ignore[arg-type]
    assert tz is timezone.utc


# Validation table — covers every False branch in validate_schedule_pattern.

@pytest.mark.parametrize(
    "pattern, expected_ok",
    [
        ({"type": "daily"}, False),
        ({"type": "daily", "times": []}, False),
        ({"type": "daily", "times": ["bogus"]}, False),
        ({"type": "daily", "times": ["09:00"]}, True),
        ({"type": "weekly"}, False),
        ({"type": "weekly", "days": []}, False),
        ({"type": "weekly", "days": ["monday"]}, False),
        ({"type": "weekly", "days": ["monday"], "times": []}, False),
        ({"type": "weekly", "days": ["funday"], "times": ["09:00"]}, False),
        ({"type": "weekly", "days": ["monday"], "times": ["nope"]}, False),
        ({"type": "weekly", "days": ["monday"], "times": ["09:00"]}, True),
        ({"type": "interval", "hours": 0, "minutes": 0}, False),
        ({}, False),  # missing type → unknown
    ],
)
def test_validate_schedule_pattern_branch_table(pattern: dict, expected_ok: bool) -> None:
    ok, _ = validate_schedule_pattern(pattern)
    assert ok is expected_ok


def test_calculate_next_run_uses_now_when_after_omitted() -> None:
    """When `after` is None, now() is used; the result must be > the call moment."""
    schedule = {"pattern": {"type": "interval", "minutes": 1}, "timezone": "UTC"}
    before = datetime.now(timezone.utc)
    out = calculate_next_run(schedule)
    assert out > before


def test_calculate_next_run_promotes_naive_after_to_utc() -> None:
    """A naive datetime is treated as UTC rather than rejected."""
    naive = datetime(2026, 4, 20, 12, 0)
    schedule = {"pattern": {"type": "interval", "minutes": 5}, "timezone": "UTC"}
    out = calculate_next_run(schedule, after=naive)
    assert out == datetime(2026, 4, 20, 12, 5, tzinfo=timezone.utc)


def test_calculate_next_run_raises_on_invalid_pattern() -> None:
    """Invalid patterns surface as ValueError from calculate_next_run, which the
    engine catches to pause the schedule."""
    schedule = {"pattern": {"type": "daily", "times": []}, "timezone": "UTC"}
    with pytest.raises(ValueError):
        calculate_next_run(schedule, after=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_calculate_next_run_weekly_jumps_across_week_boundary() -> None:
    """A Friday-only schedule queried on a Saturday rolls to the next Friday,
    exercising the multi-day search loop in _next_weekly_occurrence."""
    schedule = {
        "pattern": {"type": "weekly", "days": ["friday"], "times": ["09:00"]},
        "timezone": "UTC",
    }
    # 2026-04-25 is a Saturday.
    saturday = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    out = calculate_next_run(schedule, after=saturday)
    # Next Friday = 2026-05-01 09:00 UTC.
    assert out == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    assert out - saturday < timedelta(days=14)

