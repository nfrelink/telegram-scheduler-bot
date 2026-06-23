"""Branch coverage for scheduler.engine._process_schedule.

Each test:
- builds a real schedule + queue via DB queries
- invokes _process_schedule with a fixed `now`
- asserts side effects on DB rows + mocked bot/send_post

`send_post` returns `(ok: bool, error_text: str | None, retryable: bool)`; mocks here
return that shape directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from database import queries as db
from database.connection import get_db
from database.time import parse_timestamp as _pt
from scheduler import engine
from scheduler.rate_limiter import RateLimiter
from services import notifications


@pytest.fixture(autouse=True)
def _reset_notifications_state() -> None:
    """Per-process debounce map can suppress an admin DM in a later test
    if the previous test fired the same key first."""
    notifications.reset_debounce_state()


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
    s = dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as conn:
        await conn.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (s, schedule_id))
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
    s = dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
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
async def test_invalid_pattern_pauses_and_notifies(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8001, "001", pattern={"type": "interval", "minutes": 30})
    # Force pattern to something invalid post-creation (bypasses validation).
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET pattern = ? WHERE id = ?",
            ('{"type":"bogus"}', int(schedule["id"])),
        )
        await conn.commit()
    schedule = await _reload(int(schedule["id"]))

    send_mock = AsyncMock(return_value=(True, None, True))
    monkeypatch.setattr(engine, "send_post", send_mock)

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    user_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 8001]
    assert len(user_calls) == 1
    refreshed = await _reload(int(schedule["id"]))
    assert refreshed["state"] == "paused"


# ---------------------------------------------------------------------------
# Empty queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_queue_transitions_to_empty_paused(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8002, "002")
    send_mock = AsyncMock(return_value=(True, None, True))
    monkeypatch.setattr(engine, "send_post", send_mock)

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    user_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 8002]
    assert len(user_calls) == 1
    refreshed = await _reload(int(schedule["id"]))
    assert refreshed["state"] == "empty_paused"


# ---------------------------------------------------------------------------
# Pinned post
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_post_fires_when_pinned_at_due(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8003, "003")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.set_post_pinned_at(pid, now - timedelta(hours=1), user_id=8003)

    send_mock = AsyncMock(return_value=(True, None, True))
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
async def test_scheduled_for_in_future_does_not_fire(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8004, "004")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.bulk_update_posts_scheduled_for([(pid, now + timedelta(minutes=10))])

    send_mock = AsyncMock(return_value=(True, None, True))
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_scheduled_for_in_past_fires(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8005, "005")
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.bulk_update_posts_scheduled_for([(pid, now - timedelta(minutes=1))])

    send_mock = AsyncMock(return_value=(True, None, True))
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
    _initialized_db, monkeypatch
) -> None:
    """When next_planned_run_at is in the future, the tick does nothing."""
    schedule = await _mk(8006, "006", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await _set_next_planned_run_at(sid, now + timedelta(minutes=5))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(True, None, True))
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_fifo_fires_when_next_planned_run_at_now_or_past(
    _initialized_db, monkeypatch
) -> None:
    """When next_planned_run_at <= now, the tick fires the next post and the
    orchestrator advances NPR to the next computed slot."""
    schedule = await _mk(8007, "007", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await _set_next_planned_run_at(sid, now - timedelta(seconds=1))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(True, None, True))
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

    assert _pt(npa) == now + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Pattern-edit-then-tick must not fire a slot the new pattern places in the past
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pattern_edit_then_tick_does_not_fire_in_past_slot(
    _initialized_db, monkeypatch
) -> None:
    """After a pattern edit at 21:48, a tick at 21:49 must not fire a daily
    slot from earlier in the day (12:30). With NPR as source of truth and
    `recompute_next_run` called on edit, NPR points to the next future slot
    so the tick is a no-op."""
    schedule = await _mk(
        8008,
        "008",
        pattern={"type": "daily", "times": ["07:00", "12:30", "18:00"]},
        timezone_name="UTC",
    )
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    # Simulate the post-edit state: handler called recompute_next_run, which
    # picked the next slot strictly after the edit moment.
    edit_moment = datetime(2026, 4, 20, 21, 48, tzinfo=UTC)
    next_slot_after_edit = datetime(2026, 4, 21, 7, 0, tzinfo=UTC)
    await _set_next_planned_run_at(sid, next_slot_after_edit)
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(True, None, True))
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
async def test_null_npr_on_active_schedule_is_backfilled(_initialized_db, monkeypatch) -> None:
    """Defensive path: if an active schedule still has NULL next_planned_run_at
    (e.g. just after the migration before the first catch-up), the tick computes
    and persists a value, then exits without firing."""
    schedule = await _mk(8013, "013", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    await _set_next_planned_run_at(sid, None)
    schedule = await _reload(sid)
    assert schedule["next_planned_run_at"] is None

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    send_mock = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    refreshed = await _reload(sid)
    assert refreshed["next_planned_run_at"] is not None  # backfilled

    assert _pt(refreshed["next_planned_run_at"]) == now + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Failure / retry handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_first_retry_schedules_future_attempt(
    _initialized_db, monkeypatch
) -> None:
    schedule = await _mk(8010, "010", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await _set_next_planned_run_at(sid, now - timedelta(seconds=1))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(False, "BadRequest: chat not found", True))
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
async def test_send_failure_after_max_retries_pauses_schedule(_initialized_db, monkeypatch) -> None:
    schedule = await _mk(8011, "011", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    # Already at MAX_RETRIES; next failure tips it over.
    await db.update_post_retry(
        pid, retry_count=engine.MAX_RETRIES, scheduled_for=now - timedelta(minutes=1)
    )
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(False, "BadRequest: chat not found", True))
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    user_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 8011]
    assert len(user_calls) == 1
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
async def test_get_sleep_seconds_clamps_past_earliest_to_one_second(
    monkeypatch,
) -> None:
    """If the earliest scheduled time is already in the past, sleep = 1s so we
    pick it up on the very next tick."""
    past = datetime.now(UTC) - timedelta(seconds=30)

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
    future_far = datetime.now(UTC) + timedelta(seconds=300)

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
    npa = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    s = _bare_schedule(next_planned_run_at=npa.strftime("%Y-%m-%d %H:%M:%S"))
    assert engine._catchup_cursor(s) == npa


def test_catchup_cursor_falls_back_to_last_run_at() -> None:
    base = datetime(2026, 4, 20, 11, 0, tzinfo=UTC)
    s = _bare_schedule(last_run_at=base.strftime("%Y-%m-%d %H:%M:%S"))
    out = engine._catchup_cursor(s)
    # Interval pattern (30 min) → last_run + 30 min.
    assert out == base + timedelta(minutes=30)


def test_catchup_cursor_falls_back_to_created_at() -> None:
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    s = _bare_schedule(created_at=base.strftime("%Y-%m-%d %H:%M:%S"))
    out = engine._catchup_cursor(s)
    assert out == base + timedelta(minutes=30)


def test_catchup_cursor_returns_none_when_no_base() -> None:
    assert engine._catchup_cursor(_bare_schedule()) is None


def test_catchup_cursor_returns_none_on_invalid_pattern() -> None:
    """Invalid pattern at the calculate_next_run step → None (the catch-up loop
    skips this schedule rather than crashing)."""
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    s = _bare_schedule(
        pattern={"type": "bogus"},
        last_run_at=base.strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert engine._catchup_cursor(s) is None


# ---------------------------------------------------------------------------
# _catch_up_missed_posts — backfill / cap / exception branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catchup_backfills_npr_even_with_no_missed_runs(_initialized_db) -> None:
    """A schedule with no missed runs (NPR ahead of now) still gets its NPR
    rewritten to a fresh value. Mostly a no-op, but covers the early-continue
    path after the backfill write."""
    sched = await _mk(7100, "100", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    future_npa = datetime.now(UTC) + timedelta(hours=2)
    await _set_next_planned_run_at(sid, future_npa)

    await engine._catch_up_missed_posts()

    refreshed = await _reload(sid)

    npa = _pt(refreshed["next_planned_run_at"])
    assert npa is not None
    # Recomputed from `now`, so it's well below the original future_npa.
    assert npa < future_npa


@pytest.mark.asyncio
async def test_catchup_caps_burst_at_max_runs(_initialized_db) -> None:
    """When more slots are missed than the cap, only CATCHUP_MAX_RUNS_PER_SCHEDULE
    posts are fast-scheduled; remaining queued posts stay unscheduled (they
    drain via the regular tick at pattern cadence)."""
    sched = await _mk(7101, "101", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    # Many queued posts, well beyond the cap.
    bulk = [{"media_type": "photo", "file_id": f"f{i}"} for i in range(10)]
    await db.add_queued_posts_bulk(sid, bulk)

    # Force last_run_at far enough in the past to generate >> cap missed runs.
    past = datetime.now(UTC) - timedelta(hours=24)
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
async def test_catchup_skips_schedule_with_no_cursor(_initialized_db, monkeypatch) -> None:
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
async def test_catchup_with_missed_but_no_unscheduled_posts(_initialized_db) -> None:
    """Schedule has missed slots but the queue has no unscheduled posts (e.g.
    the queue is empty or every post already has scheduled_for set). NPR is
    still backfilled; no bulk update happens."""
    sched = await _mk(7111, "111", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    # Empty queue + stale base.
    await _set_last_run_at(sid, datetime.now(UTC) - timedelta(hours=2))
    await _set_next_planned_run_at(sid, None)

    await engine._catch_up_missed_posts()

    refreshed = await _reload(sid)
    assert refreshed["next_planned_run_at"] is not None  # backfilled


@pytest.mark.asyncio
async def test_catchup_swallows_per_schedule_exception(_initialized_db, monkeypatch) -> None:
    """If processing one schedule raises, others still run. Covers the per-
    schedule try/except in the catch-up loop."""
    sched = await _mk(7102, "102", pattern={"type": "interval", "minutes": 30})
    sid = int(sched["id"])
    await _set_last_run_at(sid, datetime.now(UTC) - timedelta(hours=2))
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
    _initialized_db, monkeypatch
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

    with caplog.at_level(logging.ERROR, logger="scheduler.engine"):
        await engine._notify_user(bot, 999, "hi", None)
    assert any("Failed to notify" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Admin DMs on unexpected pause + heartbeat
# ---------------------------------------------------------------------------
#
# These tests assert *which* events get reported to the admin and what the
# payload looks like, in addition to the user-facing DM. They use the real
# `services.notifications.notify_admin` path (not a mock) so the debounce-key
# wiring and message formatting are exercised end-to-end.


def _admin_dms_to(bot: MagicMock, admin_user_id: int) -> list[str]:
    """Extract just the message texts from `bot.send_message` calls
    addressed to the admin user id."""
    return [
        c.kwargs["text"]
        for c in bot.send_message.await_args_list
        if c.kwargs.get("chat_id") == admin_user_id
    ]


@pytest.mark.asyncio
async def test_invalid_pattern_pauses_dms_user_and_admin(_initialized_db, monkeypatch) -> None:
    """The invalid-pattern pause path emits both the existing user DM and
    a new admin DM tagged `schedule_paused_invalid_pattern`."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")

    schedule = await _mk(8101, "101", pattern={"type": "interval", "minutes": 30})
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET pattern = ? WHERE id = ?",
            ('{"type":"bogus"}', int(schedule["id"])),
        )
        await conn.commit()
    schedule = await _reload(int(schedule["id"]))

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    user_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 8101]
    admin_calls = _admin_dms_to(bot, 9999)
    assert len(user_calls) == 1
    assert len(admin_calls) == 1
    text = admin_calls[0]
    assert "schedule_paused_invalid_pattern" in text
    assert f"- schedule_id: {int(schedule['id'])}" in text
    assert "- queue_depth: 0" in text


@pytest.mark.asyncio
async def test_send_failure_after_max_retries_dms_admin_with_error(
    _initialized_db, monkeypatch
) -> None:
    """The send-failure pause path includes the threaded error text from
    `send_post` and the per-schedule debounce key."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")

    schedule = await _mk(8102, "102", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.update_post_retry(
        pid, retry_count=engine.MAX_RETRIES, scheduled_for=now - timedelta(minutes=1)
    )
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=(False, "BadRequest: chat not found", True))
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    admin_calls = _admin_dms_to(bot, 9999)
    assert len(admin_calls) == 1
    text = admin_calls[0]
    assert "schedule_paused_send_failure" in text
    assert "- last_error: BadRequest: chat not found" in text
    assert f"- post_id: {pid}" in text


@pytest.mark.asyncio
async def test_empty_queue_does_not_dm_admin(_initialized_db, monkeypatch) -> None:
    """Empty queue is the user running out of posts, not a bug; only the
    user gets DM'd. Pinning this so a future change can't silently start
    pinging the admin every time someone drains a queue."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")

    schedule = await _mk(8103, "103")
    monkeypatch.setattr(engine, "send_post", AsyncMock(return_value=(True, None, True)))

    bot = _make_bot()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert _admin_dms_to(bot, 9999) == []
    user_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == 8103]
    assert len(user_calls) == 1


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_pings_admin_when_active_schedules_are_stale(
    _initialized_db, monkeypatch
) -> None:
    """Active schedule with `last_run_at` older than HEARTBEAT_MAX_HOURS
    triggers an admin DM. Uses the real query path."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")
    monkeypatch.setattr(engine, "HEARTBEAT_MAX_HOURS", 24)

    sched = await _mk(8201, "201")
    sid = int(sched["id"])
    await _set_last_run_at(sid, datetime.now(UTC) - timedelta(hours=48))

    bot = _make_bot()
    await engine._heartbeat_check(bot, now=datetime.now(UTC), active_count=1)

    admin_calls = _admin_dms_to(bot, 9999)
    assert len(admin_calls) == 1
    assert "scheduler_heartbeat_stalled" in admin_calls[0]
    assert "- threshold_hours: 24" in admin_calls[0]


@pytest.mark.asyncio
async def test_heartbeat_silent_when_recent_run(_initialized_db, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "9999")
    monkeypatch.setattr(engine, "HEARTBEAT_MAX_HOURS", 24)

    sched = await _mk(8202, "202")
    sid = int(sched["id"])
    await _set_last_run_at(sid, datetime.now(UTC) - timedelta(hours=1))

    bot = _make_bot()
    await engine._heartbeat_check(bot, now=datetime.now(UTC), active_count=1)

    assert _admin_dms_to(bot, 9999) == []


@pytest.mark.asyncio
async def test_heartbeat_silent_when_no_active_schedules(monkeypatch) -> None:
    """No active schedules means nothing should be firing — silence is the
    correct signal, not a stalled-loop alarm."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")

    bot = _make_bot()
    await engine._heartbeat_check(bot, now=datetime.now(UTC), active_count=0)

    assert _admin_dms_to(bot, 9999) == []


@pytest.mark.asyncio
async def test_heartbeat_silent_when_active_but_never_fired(_initialized_db, monkeypatch) -> None:
    """A freshly-deployed bot with active schedules and no `last_run_at`
    yet must not ping the admin on the very first tick — let the value
    populate naturally."""
    monkeypatch.setenv("ADMIN_USER_ID", "9999")

    await _mk(8203, "203")  # last_run_at is NULL

    bot = _make_bot()
    await engine._heartbeat_check(bot, now=datetime.now(UTC), active_count=1)

    assert _admin_dms_to(bot, 9999) == []


# ---------------------------------------------------------------------------
# Structured-logging guard tests
#
# These exist so that a future refactor cannot silently drop the `event=`
# tag from the two log records production observability depends on most:
# `post_sent` (the heartbeat of healthy operation) and
# `schedule_paused_send_failure` (the loudest "something is wrong" signal).
# The JSON formatter in `logging_setup` promotes anything in `extra` to a
# top-level field; that's what `jq 'select(.event=="...")'` filters on.
# ---------------------------------------------------------------------------


def _records_with_event(caplog, event: str) -> list:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.mark.asyncio
async def test_post_sent_log_carries_structured_event(_initialized_db, monkeypatch, caplog) -> None:
    schedule = await _mk(8301, "301", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await _set_next_planned_run_at(sid, now - timedelta(seconds=1))
    schedule = await _reload(sid)

    monkeypatch.setattr(engine, "send_post", AsyncMock(return_value=(True, None, True)))
    bot = _make_bot()

    with caplog.at_level("INFO", logger="scheduler.engine"):
        await engine._process_schedule(
            bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
        )

    records = _records_with_event(caplog, "post_sent")
    assert len(records) == 1, "post_sent record missing or duplicated"
    rec = records[0]
    assert rec.schedule_id == sid
    assert rec.channel_id == "-100301"
    assert rec.post_id is not None


@pytest.mark.asyncio
async def test_schedule_paused_send_failure_log_carries_structured_event(
    _initialized_db, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "9999")
    schedule = await _mk(8302, "302", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])
    posts = await db.get_queued_posts(sid, limit=1)
    pid = int(posts[0]["id"])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    await db.update_post_retry(
        pid, retry_count=engine.MAX_RETRIES, scheduled_for=now - timedelta(minutes=1)
    )
    schedule = await _reload(sid)

    monkeypatch.setattr(
        engine,
        "send_post",
        AsyncMock(return_value=(False, "BadRequest: chat not found", True)),
    )
    bot = _make_bot()

    with caplog.at_level("ERROR", logger="scheduler.engine"):
        await engine._process_schedule(
            bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
        )

    records = _records_with_event(caplog, "schedule_paused_send_failure")
    assert len(records) == 1, "schedule_paused_send_failure record missing or duplicated"
    rec = records[0]
    assert rec.schedule_id == sid
    assert rec.post_id == pid
    assert rec.retry_count == engine.MAX_RETRIES + 1
    assert "chat not found" in (rec.last_error or "")
