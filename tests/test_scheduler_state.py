"""Branch coverage for scheduler.state.recompute_next_run."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from database import queries as db
from database.connection import get_db
from database.time import parse_timestamp
from scheduler.state import recompute_next_run


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


@pytest.mark.asyncio
async def test_recompute_active_schedule_writes_npr(initialized_db) -> None:
    sid = await _mk_schedule(7300, "300", pattern={"type": "interval", "minutes": 30})
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    out = await recompute_next_run(sid, now=now)

    assert out == now + timedelta(minutes=30)
    sched = await db.get_schedule(sid)
    assert parse_timestamp(sched["next_planned_run_at"]) == now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_recompute_paused_schedule_clears_npr(initialized_db) -> None:
    """A non-active schedule must end up with NPR = NULL, regardless of any
    previous value."""
    sid = await _mk_schedule(
        7301, "301", pattern={"type": "interval", "minutes": 30}, state="paused"
    )
    # Seed a stale value first.
    seeded = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await db.update_schedule_next_planned_run(sid, seeded)
    assert (await db.get_schedule(sid))["next_planned_run_at"] is not None

    out = await recompute_next_run(sid)

    assert out is None
    sched = await db.get_schedule(sid)
    assert sched["next_planned_run_at"] is None


@pytest.mark.asyncio
async def test_recompute_missing_schedule_returns_none(initialized_db) -> None:
    out = await recompute_next_run(999_999)
    assert out is None


@pytest.mark.asyncio
async def test_recompute_invalid_pattern_clears_npr_and_warns(
    initialized_db, caplog
) -> None:
    """An active schedule with a corrupted pattern (bypassing validation) must
    not crash the caller; instead recompute clears NPR and emits a warning so
    the engine's tick can later transition it to paused."""
    sid = await _mk_schedule(7302, "302", pattern={"type": "interval", "minutes": 30})
    # Corrupt the pattern post-hoc.
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET pattern = ? WHERE id = ?",
            ('{"type":"bogus"}', sid),
        )
        await conn.commit()
    # Pre-seed NPR to make the clearing observable.
    await db.update_schedule_next_planned_run(
        sid, datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    )

    with caplog.at_level(logging.WARNING, logger="scheduler.state"):
        out = await recompute_next_run(sid)

    assert out is None
    sched = await db.get_schedule(sid)
    assert sched["next_planned_run_at"] is None
    assert any("Cannot compute next run" in rec.message for rec in caplog.records)
