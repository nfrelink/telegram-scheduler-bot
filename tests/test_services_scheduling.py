"""Tests for services.scheduling — lifecycle wrappers + recompute."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from database import queries as db
from database.connection import get_db
from database.time import parse_timestamp
from services import scheduling

# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------


async def _mk_schedule(
    user_id: int,
    suffix: str,
    *,
    pattern: dict,
    state: str = "active",
    timezone_name: str = "UTC",
) -> int:
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    ch = await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-100{suffix}",
        channel_name=f"C-{suffix}",
    )
    s = await db.create_schedule(
        channel_db_id=int(ch["id"]),
        name=f"S-{suffix}",
        pattern=pattern,
        timezone_name=timezone_name,
        state=state,
    )
    return int(s["id"])


# ---------------------------------------------------------------------------
# next_planned_for (pure)
# ---------------------------------------------------------------------------


def test_next_planned_for_paused_returns_none() -> None:
    schedule = {
        "id": 1,
        "state": "paused",
        "pattern": {"type": "interval", "minutes": 5},
        "timezone": "UTC",
    }
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    assert scheduling.next_planned_for(schedule, after=now) is None


def test_next_planned_for_active_returns_calculation() -> None:
    schedule = {
        "id": 1,
        "state": "active",
        "pattern": {"type": "interval", "minutes": 5},
        "timezone": "UTC",
    }
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    out = scheduling.next_planned_for(schedule, after=now)
    assert out == now + timedelta(minutes=5)


def test_next_planned_for_invalid_pattern_returns_none(caplog) -> None:
    schedule = {
        "id": 42,
        "state": "active",
        "pattern": {"type": "bogus"},
        "timezone": "UTC",
    }
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger="services.scheduling"):
        out = scheduling.next_planned_for(schedule, after=now)
    assert out is None
    assert any("Cannot compute next run" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# recompute_next_run / persist_next_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_active_schedule_writes_npr(initialized_db) -> None:
    sid = await _mk_schedule(7300, "300", pattern={"type": "interval", "minutes": 30})
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    out = await scheduling.recompute_next_run(sid, now=now)
    assert out == now + timedelta(minutes=30)
    sched = await db.get_schedule(sid)
    assert parse_timestamp(sched["next_planned_run_at"]) == now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_recompute_paused_schedule_clears_npr(initialized_db) -> None:
    """Non-active schedules end up with NPR = NULL regardless of any prior value."""
    sid = await _mk_schedule(
        7301, "301", pattern={"type": "interval", "minutes": 30}, state="paused"
    )
    seeded = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.update_schedule_next_planned_run(sid, seeded)
    assert (await db.get_schedule(sid))["next_planned_run_at"] is not None
    out = await scheduling.recompute_next_run(sid)
    assert out is None
    sched = await db.get_schedule(sid)
    assert sched["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_recompute_missing_schedule_returns_none(initialized_db) -> None:
    out = await scheduling.recompute_next_run(999_999)
    assert out is None


@pytest.mark.asyncio
async def test_recompute_invalid_pattern_clears_npr(initialized_db) -> None:
    """An active schedule with a corrupted pattern (bypassing validation) must
    not crash recompute; NPR is cleared so the engine's next tick can pause it."""
    sid = await _mk_schedule(7302, "302", pattern={"type": "interval", "minutes": 30})
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET pattern = ? WHERE id = ?",
            ('{"type":"bogus"}', sid),
        )
        await conn.commit()
    await db.update_schedule_next_planned_run(sid, datetime(2026, 4, 20, 12, 0, tzinfo=UTC))
    out = await scheduling.recompute_next_run(sid)
    assert out is None
    sched = await db.get_schedule(sid)
    assert sched["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_persist_next_run_writes_value(initialized_db) -> None:
    sid = await _mk_schedule(7303, "303", pattern={"type": "interval", "minutes": 30})
    when = datetime(2026, 4, 20, 13, 0, tzinfo=UTC)
    await scheduling.persist_next_run(sid, when)
    sched = await db.get_schedule(sid)
    assert parse_timestamp(sched["next_planned_run_at"]) == when


# ---------------------------------------------------------------------------
# Lifecycle wrappers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_defaults_to_paused_with_null_npr(initialized_db) -> None:
    user_id = 7400
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-7400", channel_name="C")
    s = await scheduling.create(
        channel_db_id=int(ch["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
    )
    assert s["state"] == "paused"
    assert s["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_pause_clears_npr(initialized_db) -> None:
    user_id = 7401
    sid = await _mk_schedule(user_id, "401", pattern={"type": "interval", "minutes": 5})
    await db.update_schedule_next_planned_run(sid, datetime(2026, 4, 20, 12, 0, tzinfo=UTC))
    await scheduling.pause(sid, user_id=user_id)
    sched = await db.get_schedule(sid)
    assert sched["state"] == "paused"
    assert sched["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_resume_recomputes_npr(initialized_db) -> None:
    user_id = 7402
    sid = await _mk_schedule(
        user_id, "402", pattern={"type": "interval", "minutes": 5}, state="paused"
    )
    await scheduling.resume(sid, user_id=user_id)
    sched = await db.get_schedule(sid)
    assert sched["state"] == "active"
    # NPR must be a value strictly after creation; we don't pin to an exact
    # second since `now` was sampled inside the service.
    assert parse_timestamp(sched["next_planned_run_at"]) is not None


@pytest.mark.asyncio
async def test_update_pattern_recomputes_npr(initialized_db) -> None:
    """The 23:48-edit-then-tick scenario: editing the pattern must move NPR
    to a future slot so the next tick does not fire a back-dated time."""
    user_id = 7403
    sid = await _mk_schedule(user_id, "403", pattern={"type": "daily", "times": ["07:00", "18:00"]})
    # Stale NPR pointing into the past.
    stale = datetime.now(UTC) - timedelta(hours=1)
    await db.update_schedule_next_planned_run(sid, stale)

    await scheduling.update_pattern(
        sid,
        {"type": "daily", "times": ["07:00", "12:30", "18:00"]},
        user_id=user_id,
    )
    sched = await db.get_schedule(sid)
    npr = parse_timestamp(sched["next_planned_run_at"])
    assert npr is not None
    assert npr > datetime.now(UTC) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_update_timezone_recomputes_npr(initialized_db) -> None:
    user_id = 7404
    sid = await _mk_schedule(user_id, "404", pattern={"type": "daily", "times": ["12:00"]})
    await scheduling.update_timezone(sid, timezone_name="America/New_York", user_id=user_id)
    sched = await db.get_schedule(sid)
    assert sched["timezone"] == "America/New_York"
    assert parse_timestamp(sched["next_planned_run_at"]) is not None


@pytest.mark.asyncio
async def test_update_name_does_not_touch_npr(initialized_db) -> None:
    user_id = 7405
    sid = await _mk_schedule(user_id, "405", pattern={"type": "interval", "minutes": 5})
    when = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.update_schedule_next_planned_run(sid, when)
    await scheduling.update_name(sid, name="Renamed", user_id=user_id)
    sched = await db.get_schedule(sid)
    assert sched["name"] == "Renamed"
    assert parse_timestamp(sched["next_planned_run_at"]) == when


@pytest.mark.asyncio
async def test_mark_empty_clears_npr(initialized_db) -> None:
    user_id = 7406
    sid = await _mk_schedule(user_id, "406", pattern={"type": "interval", "minutes": 5})
    await db.update_schedule_next_planned_run(sid, datetime(2026, 4, 20, 12, 0, tzinfo=UTC))
    await scheduling.mark_empty(sid, user_id=user_id)
    sched = await db.get_schedule(sid)
    assert sched["state"] == "empty_paused"
    assert sched["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_delete_removes_schedule(initialized_db) -> None:
    user_id = 7407
    sid = await _mk_schedule(user_id, "407", pattern={"type": "interval", "minutes": 5})
    await scheduling.delete(sid, user_id=user_id)
    assert await db.get_schedule(sid) is None


# ---------------------------------------------------------------------------
# Timezone validation at service boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_invalid_timezone(initialized_db) -> None:
    """Handlers validate first, but the service is the source of truth. A
    handler-bypassing caller must not be able to write an invalid tz."""
    user_id = 7500
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-7500", channel_name="C")

    with pytest.raises(scheduling.InvalidTimezoneError) as excinfo:
        await scheduling.create(
            channel_db_id=int(ch["id"]),
            name="S",
            pattern={"type": "interval", "minutes": 5},
            timezone_name="Foo/Bar",
        )
    assert "Foo/Bar" in str(excinfo.value)

    # Nothing was persisted.
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM schedules WHERE channel_id = ?",
            (int(ch["id"]),),
        )
        row = await cursor.fetchone()
        assert int(row["n"]) == 0


@pytest.mark.asyncio
async def test_create_accepts_canonical_utc(initialized_db) -> None:
    """UTC must survive validation even on systems without tzdata; this
    pins the `utils.tz.is_valid_timezone` UTC short-circuit at the service
    boundary."""
    user_id = 7501
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-7501", channel_name="C")
    s = await scheduling.create(
        channel_db_id=int(ch["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
    )
    assert s["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_update_timezone_rejects_invalid_timezone(initialized_db) -> None:
    user_id = 7502
    sid = await _mk_schedule(user_id, "502", pattern={"type": "daily", "times": ["12:00"]})

    with pytest.raises(scheduling.InvalidTimezoneError) as excinfo:
        await scheduling.update_timezone(sid, timezone_name="Not/ARealZone", user_id=user_id)
    assert "Not/ARealZone" in str(excinfo.value)

    # The stored tz must be unchanged on rejection.
    sched = await db.get_schedule(sid)
    assert sched["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_update_timezone_exception_carries_suggestions(initialized_db) -> None:
    """A close typo produces an actionable error the handler can forward
    verbatim."""
    user_id = 7503
    sid = await _mk_schedule(user_id, "503", pattern={"type": "daily", "times": ["12:00"]})

    with pytest.raises(scheduling.InvalidTimezoneError) as excinfo:
        await scheduling.update_timezone(sid, timezone_name="Europe/Amsterdamm", user_id=user_id)
    assert "Europe/Amsterdam" in excinfo.value.suggestions
    assert "Did you mean" in str(excinfo.value)
