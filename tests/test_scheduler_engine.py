"""Branch coverage for scheduler.engine._process_schedule.

Each test:
- builds a real schedule + queue via DB queries
- invokes _process_schedule with a fixed `now`
- asserts side effects on DB rows + mocked bot/send_post
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from database import queries as db
from database.connection import get_db
from scheduler import engine
from scheduler.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mk(
    user_id: int,
    suffix: str,
    *,
    pattern: dict | None = None,
    state: str = "active",
    timezone_name: str = "UTC",
) -> dict:
    """Create user + channel + schedule and return the joined schedule dict
    in the same shape _process_schedule consumes (via get_active_schedules)."""
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-100{suffix}",
        channel_name=f"Chan-{suffix}",
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name=f"Sched-{suffix}",
        pattern=pattern or {"type": "interval", "minutes": 30},
        timezone_name=timezone_name,
        state=state,
    )
    full = await db.get_schedule_with_channel(int(schedule["id"]))
    assert full is not None
    return full


async def _set_last_run_at(schedule_id: int, dt: datetime) -> None:
    """Force schedules.last_run_at to a specific UTC time (bypasses NOW())."""
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?", (s, schedule_id)
        )
        await conn.commit()


async def _set_next_planned_run_at(schedule_id: int, dt: datetime | None) -> None:
    """Force schedules.next_planned_run_at to a specific UTC time, or NULL."""
    if dt is None:
        async with get_db() as conn:
            await conn.execute(
                "UPDATE schedules SET next_planned_run_at = NULL WHERE id = ?",
                (schedule_id,),
            )
            await conn.commit()
        return
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET next_planned_run_at = ? WHERE id = ?",
            (s, schedule_id),
        )
        await conn.commit()


def _make_bot() -> MagicMock:
    bot = MagicMock(name="bot")
    bot.send_message = AsyncMock(return_value=None)
    return bot


async def _reload(schedule_id: int) -> dict:
    s = await db.get_schedule_with_channel(schedule_id)
    assert s is not None
    return s


# ---------------------------------------------------------------------------
# Pattern validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_pattern_pauses_and_notifies(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8001, "001", pattern={"type": "interval", "minutes": 30})
    # Force pattern to something invalid post-creation (bypasses validation).
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET pattern = ? WHERE id = ?",
            ('{"type":"bogus"}', int(schedule["id"])),
        )
        await conn.commit()
    schedule = await _reload(int(schedule["id"]))

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    bot.send_message.assert_awaited_once()
    refreshed = await _reload(int(schedule["id"]))
    assert refreshed["state"] == "paused"


# ---------------------------------------------------------------------------
# Empty queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_queue_transitions_to_empty_paused(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8002, "002")
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    bot.send_message.assert_awaited_once()
    refreshed = await _reload(int(schedule["id"]))
    assert refreshed["state"] == "empty_paused"


# ---------------------------------------------------------------------------
# Pinned post
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pinned_post_fires_when_pinned_at_due(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8003, "003")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await db.set_post_pinned_at(pid, now - timedelta(hours=1), user_id=8003)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    remaining = await db.get_queued_posts(sid, limit=10)
    assert remaining == []
    refreshed = await _reload(sid)
    assert refreshed["last_run_at"] is not None


# ---------------------------------------------------------------------------
# scheduled_for (catch-up / retry slot)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduled_for_in_future_does_not_fire(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8004, "004")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await db.bulk_update_posts_scheduled_for([(pid, now + timedelta(minutes=10))])

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_scheduled_for_in_past_fires(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8005, "005")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await db.bulk_update_posts_scheduled_for([(pid, now - timedelta(minutes=1))])

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    assert await db.get_queued_posts(sid, limit=10) == []


# ---------------------------------------------------------------------------
# Normal FIFO timing — gated by next_planned_run_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fifo_does_not_fire_when_next_planned_run_in_future(
    initialized_db, monkeypatch
) -> None:
    """When next_planned_run_at is in the future, the tick does nothing."""
    schedule = await _mk(8006, "006", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await _set_next_planned_run_at(sid, now + timedelta(minutes=5))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_fifo_fires_when_next_planned_run_at_now_or_past(
    initialized_db, monkeypatch
) -> None:
    """When next_planned_run_at <= now, the tick fires the next post and the
    orchestrator advances NPR to the next computed slot."""
    schedule = await _mk(8007, "007", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await _set_next_planned_run_at(sid, now - timedelta(seconds=1))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    assert await db.get_queued_posts(sid, limit=10) == []
    refreshed = await _reload(sid)
    # Interval pattern: NPR after fire is now + 30 min.
    npa = refreshed["next_planned_run_at"]
    assert npa is not None
    from database.time import parse_timestamp as _pt
    assert _pt(npa) == now + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Pattern-edit-then-tick (the bug Phase 1.1 structurally prevents)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pattern_edit_then_tick_does_not_fire_in_past_slot(
    initialized_db, monkeypatch
) -> None:
    """After a pattern edit at 21:48, a tick at 21:49 must not fire a daily
    slot from earlier in the day (12:30). With NPR as source of truth and
    `recompute_next_run` called on edit, NPR points to the next future slot
    so the tick is a no-op."""
    schedule = await _mk(
        8008, "008",
        pattern={"type": "daily", "times": ["07:00", "12:30", "18:00"]},
        timezone_name="UTC",
    )
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    # Simulate the post-edit state: handler called recompute_next_run, which
    # picked the next slot strictly after the edit moment.
    edit_moment = datetime(2026, 4, 20, 21, 48, tzinfo=timezone.utc)
    next_slot_after_edit = datetime(2026, 4, 21, 7, 0, tzinfo=timezone.utc)
    await _set_next_planned_run_at(sid, next_slot_after_edit)
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    # Tick one minute after the edit; NPR is still > now, so no fire.
    tick_now = edit_moment + timedelta(minutes=1)
    await engine._process_schedule(
        bot, schedule, now=tick_now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_null_npr_on_active_schedule_is_backfilled(
    initialized_db, monkeypatch
) -> None:
    """Defensive path: if an active schedule still has NULL next_planned_run_at
    (e.g. just after the migration before the first catch-up), the tick computes
    and persists a value, then exits without firing."""
    schedule = await _mk(8013, "013", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    await _set_next_planned_run_at(sid, None)
    schedule = await _reload(sid)
    assert schedule["next_planned_run_at"] is None

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    refreshed = await _reload(sid)
    assert refreshed["next_planned_run_at"] is not None  # backfilled
    from database.time import parse_timestamp as _pt
    assert _pt(refreshed["next_planned_run_at"]) == now + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Failure / retry handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_failure_first_retry_schedules_future_attempt(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8010, "010", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await _set_next_planned_run_at(sid, now - timedelta(seconds=1))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    refreshed = await _reload(sid)
    assert refreshed["state"] == "active"
    posts_after = await db.get_queued_posts(sid, limit=1)
    assert int(posts_after[0]["id"]) == pid
    assert int(posts_after[0]["retry_count"]) == 1
    assert posts_after[0]["scheduled_for"] is not None


@pytest.mark.asyncio
async def test_send_failure_after_max_retries_pauses_schedule(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8011, "011", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    # Already at MAX_RETRIES; next failure tips it over.
    await db.update_post_retry(
        pid, retry_count=engine.MAX_RETRIES, scheduled_for=now - timedelta(minutes=1)
    )
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    refreshed = await _reload(sid)
    assert refreshed["state"] == "paused"


# ---------------------------------------------------------------------------
# _get_sleep_seconds — branches around earliest-pinned/scheduled lookups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sleep_seconds_defaults_when_no_earliest(monkeypatch) -> None:
    """No scheduled_for and no pinned_at anywhere → return the default."""
    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(db, "get_earliest_scheduled_for", _none)
    monkeypatch.setattr(db, "get_earliest_pinned_at", _none)

    assert await engine._get_sleep_seconds(60) == 60.0


@pytest.mark.asyncio
async def test_get_sleep_seconds_swallows_getter_exceptions(monkeypatch) -> None:
    """If a getter raises, the function treats that source as 'no earliest'.
    Ensures one flaky table doesn't crash the scheduler tick."""
    async def _boom(*_a, **_k):
        raise RuntimeError("db gone")

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(db, "get_earliest_scheduled_for", _boom)
    monkeypatch.setattr(db, "get_earliest_pinned_at", _none)

    assert await engine._get_sleep_seconds(45) == 45.0


@pytest.mark.asyncio
async def test_get_sleep_seconds_clamps_past_earliest_to_one_second(monkeypatch) -> None:
    """If the earliest scheduled time is already in the past, sleep = 1s so we
    pick it up on the very next tick."""
    past = datetime.now(timezone.utc) - timedelta(seconds=30)

    async def _past(*_a, **_k):
        return past.strftime("%Y-%m-%d %H:%M:%S")

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(db, "get_earliest_scheduled_for", _past)
    monkeypatch.setattr(db, "get_earliest_pinned_at", _none)

    assert await engine._get_sleep_seconds(60) == 1.0


@pytest.mark.asyncio
async def test_get_sleep_seconds_caps_at_default(monkeypatch) -> None:
    """A future earliest > default → use default; smaller → use that."""
    future_far = datetime.now(timezone.utc) + timedelta(seconds=300)

    async def _far(*_a, **_k):
        return future_far.strftime("%Y-%m-%d %H:%M:%S")

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(db, "get_earliest_scheduled_for", _far)
    monkeypatch.setattr(db, "get_earliest_pinned_at", _none)

    s = await engine._get_sleep_seconds(60)
    assert s == 60.0


# ---------------------------------------------------------------------------
# _catchup_cursor — branch table for cursor selection
# ---------------------------------------------------------------------------

def _bare_schedule(**overrides) -> dict:
    base = {
        "id": 0,
        "pattern": {"type": "interval", "minutes": 30},
        "timezone": "UTC",
        "next_planned_run_at": None,
        "last_run_at": None,
        "created_at": None,
    }
    base.update(overrides)
    return base


def test_catchup_cursor_prefers_next_planned_run_at() -> None:
    npa = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    s = _bare_schedule(next_planned_run_at=npa.strftime("%Y-%m-%d %H:%M:%S"))
    assert engine._catchup_cursor(s, now=datetime.now(timezone.utc)) == npa


def test_catchup_cursor_falls_back_to_last_run_at() -> None:
    base = datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc)
    s = _bare_schedule(last_run_at=base.strftime("%Y-%m-%d %H:%M:%S"))
    out = engine._catchup_cursor(s, now=datetime.now(timezone.utc))
    # Interval pattern (30 min) → last_run + 30 min.
    assert out == base + timedelta(minutes=30)


def test_catchup_cursor_falls_back_to_created_at() -> None:
    base = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
    s = _bare_schedule(created_at=base.strftime("%Y-%m-%d %H:%M:%S"))
    out = engine._catchup_cursor(s, now=datetime.now(timezone.utc))
    assert out == base + timedelta(minutes=30)


def test_catchup_cursor_returns_none_when_no_base() -> None:
    assert engine._catchup_cursor(_bare_schedule(), now=datetime.now(timezone.utc)) is None


def test_catchup_cursor_returns_none_on_invalid_pattern() -> None:
    """Invalid pattern at the calculate_next_run step → None (the catch-up loop
    skips this schedule rather than crashing)."""
    base = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
    s = _bare_schedule(
        pattern={"type": "bogus"},
        last_run_at=base.strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert engine._catchup_cursor(s, now=datetime.now(timezone.utc)) is None


# ---------------------------------------------------------------------------
# _catch_up_missed_posts — backfill / cap / exception branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catchup_backfills_npr_even_with_no_missed_runs(initialized_db) -> None:
    """A schedule with no missed runs (NPR ahead of now) still gets its NPR
    rewritten to a fresh value. Mostly a no-op, but covers the early-continue
    path after the backfill write."""
    sched = await _mk(7100, "100", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    future_npa = datetime.now(timezone.utc) + timedelta(hours=2)
    await _set_next_planned_run_at(sid, future_npa)

    await engine._catch_up_missed_posts()

    refreshed = await _reload(sid)
    from database.time import parse_timestamp as _pt
    npa = _pt(refreshed["next_planned_run_at"])
    assert npa is not None
    # Recomputed from `now`, so it's well below the original future_npa.
    assert npa < future_npa


@pytest.mark.asyncio
async def test_catchup_caps_burst_at_max_runs(initialized_db) -> None:
    """When more slots are missed than the cap, only CATCHUP_MAX_RUNS_PER_SCHEDULE
    posts are fast-scheduled; remaining queued posts stay unscheduled (they
    drain via the regular tick at pattern cadence)."""
    sched = await _mk(7101, "101", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    # Many queued posts, well beyond the cap.
    bulk = [{"media_type": "photo", "file_id": f"f{i}"} for i in range(10)]
    await db.add_queued_posts_bulk(sid, bulk)

    # Force last_run_at far enough in the past to generate >> cap missed runs.
    past = datetime.now(timezone.utc) - timedelta(hours=24)
    await _set_last_run_at(sid, past)
    # Ensure NPR null so we hit the fallback cursor path.
    await _set_next_planned_run_at(sid, None)

    await engine._catch_up_missed_posts()

    posts = await db.get_queued_posts(sid, limit=20)
    scheduled = [p for p in posts if p.get("scheduled_for") is not None]
    unscheduled = [p for p in posts if p.get("scheduled_for") is None]
    assert len(scheduled) == engine.CATCHUP_MAX_RUNS_PER_SCHEDULE
    assert len(unscheduled) == 10 - engine.CATCHUP_MAX_RUNS_PER_SCHEDULE


@pytest.mark.asyncio
async def test_catchup_skips_schedule_with_no_cursor(initialized_db, monkeypatch) -> None:
    """If _catchup_cursor returns None for a schedule, that schedule is skipped
    entirely (no NPR write, no candidate fetch). Covers the early-continue."""
    sched = await _mk(7110, "110", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    await _set_next_planned_run_at(sid, None)

    # Force cursor None for any schedule.
    monkeypatch.setattr(engine, "_catchup_cursor", lambda *_a, **_k: None)

    # Spy on the NPR write to make sure it isn't called for this schedule.
    write_calls: list[int] = []

    real_write = db.update_schedule_next_planned_run

    async def _spy_write(schedule_id, value):
        write_calls.append(schedule_id)
        await real_write(schedule_id, value)

    monkeypatch.setattr(db, "update_schedule_next_planned_run", _spy_write)

    await engine._catch_up_missed_posts()

    assert sid not in write_calls


@pytest.mark.asyncio
async def test_catchup_with_missed_but_no_unscheduled_posts(initialized_db) -> None:
    """Schedule has missed slots but the queue has no unscheduled posts (e.g.
    the queue is empty or every post already has scheduled_for set). NPR is
    still backfilled; no bulk update happens."""
    sched = await _mk(7111, "111", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    # Empty queue + stale base.
    await _set_last_run_at(sid, datetime.now(timezone.utc) - timedelta(hours=2))
    await _set_next_planned_run_at(sid, None)

    await engine._catch_up_missed_posts()

    refreshed = await _reload(sid)
    assert refreshed["next_planned_run_at"] is not None  # backfilled


@pytest.mark.asyncio
async def test_catchup_swallows_per_schedule_exception(initialized_db, monkeypatch) -> None:
    """If processing one schedule raises, others still run. Covers the per-
    schedule try/except in the catch-up loop."""
    sched = await _mk(7102, "102", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    await _set_last_run_at(sid, datetime.now(timezone.utc) - timedelta(hours=2))
    await _set_next_planned_run_at(sid, None)

    # Force an exception inside the loop on the bulk update step.
    async def _boom(*_a, **_k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(db, "bulk_update_posts_scheduled_for", _boom)
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "x"}])

    # Should not raise.
    await engine._catch_up_missed_posts()


# ---------------------------------------------------------------------------
# _process_due_schedules — swallows per-schedule exceptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_due_schedules_swallows_per_schedule_exception(
    initialized_db, monkeypatch
) -> None:
    """Forces _process_schedule to raise; the outer loop must log and move on."""
    await _mk(7200, "200", pattern={"type": "interval", "minutes": 30})

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated tick failure")

    monkeypatch.setattr(engine, "_process_schedule", _boom)

    bot = _make_bot()
    # Should return without raising, despite the inner explosion.
    await engine._process_due_schedules(bot, rate_limiter=RateLimiter(min_interval_seconds=0))


# ---------------------------------------------------------------------------
# _notify_user — exception swallowing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_user_swallows_send_message_failure(caplog) -> None:
    """If bot.send_message raises (e.g. user blocked the bot), _notify_user
    logs and returns rather than propagating."""
    bot = _make_bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("blocked"))

    import logging
    with caplog.at_level(logging.ERROR, logger="scheduler.engine"):
        await engine._notify_user(bot, 999, "hi", None)
    assert any("Failed to notify" in rec.message for rec in caplog.records)
