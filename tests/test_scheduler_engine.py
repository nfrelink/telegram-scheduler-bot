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
# Normal FIFO timing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fifo_does_not_fire_when_next_run_in_future(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8006, "006", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await _set_last_run_at(sid, now - timedelta(minutes=5))
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
async def test_fifo_fires_when_next_run_due(initialized_db, monkeypatch) -> None:
    schedule = await _mk(8007, "007", pattern={"type": "interval", "minutes": 30})
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await _set_last_run_at(sid, now - timedelta(minutes=31))
    schedule = await _reload(sid)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()
    assert await db.get_queued_posts(sid, limit=10) == []


# ---------------------------------------------------------------------------
# Daily grace-window clamp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_grace_clamp_suppresses_in_past_slot(initialized_db, monkeypatch) -> None:
    """A daily pattern with a stale last_run_at must NOT fire a wall-clock slot
    that has already passed today. The grace clamp moves base_after forward to
    `now - FIFO_LOOKBACK_GRACE_SECONDS` so calculate_next_run picks the next
    future slot instead of an in-past one."""
    schedule = await _mk(
        8008, "008",
        pattern={"type": "daily", "times": ["07:00", "12:30", "18:00"]},
        timezone_name="UTC",
    )
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    # now is well past the 12:30 slot; last_run_at is stale enough that without
    # the clamp, _next_daily_occurrence would return 12:30 today (in the past).
    now = datetime(2026, 4, 20, 21, 48, tzinfo=timezone.utc)
    await _set_last_run_at(sid, datetime(2026, 4, 20, 10, 48, tzinfo=timezone.utc))
    schedule = await _reload(sid)

    monkeypatch.setattr(engine, "FIFO_LOOKBACK_GRACE_SECONDS", 300)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    assert send_mock.await_count == 0
    assert len(await db.get_queued_posts(sid, limit=10)) == 1


@pytest.mark.asyncio
async def test_daily_no_clamp_fires_in_past_slot(initialized_db, monkeypatch) -> None:
    """Inverse: when FIFO_LOOKBACK_GRACE_SECONDS is large enough that the clamp
    is a no-op (cutoff falls before base_after), the same setup *does* fire the
    in-past 12:30 slot. Confirms the clamp in the test above is what suppresses
    the fire — not some unrelated guard."""
    schedule = await _mk(
        8009, "009",
        pattern={"type": "daily", "times": ["07:00", "12:30", "18:00"]},
        timezone_name="UTC",
    )
    sid = int(schedule["id"])
    await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "p1"}])

    now = datetime(2026, 4, 20, 21, 48, tzinfo=timezone.utc)
    await _set_last_run_at(sid, datetime(2026, 4, 20, 10, 48, tzinfo=timezone.utc))
    schedule = await _reload(sid)

    # Grace large enough that cutoff < base_after, so the clamp does nothing.
    monkeypatch.setattr(engine, "FIFO_LOOKBACK_GRACE_SECONDS", 10**9)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "send_post", send_mock)
    bot = _make_bot()

    await engine._process_schedule(
        bot, schedule, now=now, rate_limiter=RateLimiter(min_interval_seconds=0)
    )

    send_mock.assert_awaited_once()


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
    await _set_last_run_at(sid, now - timedelta(minutes=31))
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
