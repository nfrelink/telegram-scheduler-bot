from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import queries as db
from database.connection import get_db
from scheduler import engine


@pytest.mark.asyncio
async def test_catch_up_sets_scheduled_for_with_spacing(initialized_db) -> None:
    user_id = 1000
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-3003", channel_name="Channel 3")

    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="CatchUp",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="active",
    )
    schedule_id = int(schedule["id"])

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "f1"},
            {"media_type": "photo", "file_id": "f2"},
            {"media_type": "photo", "file_id": "f3"},
        ],
    )

    # Force created_at far enough in the past to guarantee missed runs (cap is 20).
    past = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0)
    past_str = past.strftime("%Y-%m-%d %H:%M:%S")

    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET created_at = ?, last_run_at = NULL WHERE id = ?",
            (past_str, schedule_id),
        )
        await conn.commit()

    await engine._catch_up_missed_posts()

    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert len(posts) == 3

    scheduled = [engine._parse_timestamp(p.get("scheduled_for")) for p in posts]
    assert all(s is not None for s in scheduled)

    t0, t1, t2 = scheduled[0], scheduled[1], scheduled[2]
    assert int((t1 - t0).total_seconds()) == engine.CATCHUP_SPACING_SECONDS  # type: ignore[operator]
    assert int((t2 - t1).total_seconds()) == engine.CATCHUP_SPACING_SECONDS  # type: ignore[operator]

    # Scheduler sleep should be <= default when something is scheduled soon.
    sleep_s = await engine._get_sleep_seconds(60)
    assert 1.0 <= sleep_s <= 60.0

