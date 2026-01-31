from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import transaction
from database import queries as db
from database.time import to_sqlite_timestamp
from scheduler.engine import _parse_timestamp


@pytest.mark.asyncio
async def test_add_queued_posts_bulk_appends_and_compacts_positions(initialized_db) -> None:
    user_id = 123
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-1001", channel_name="Test Channel")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Test Schedule",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    inserted1 = await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "a", "caption": "c1"},
            {"media_type": "photo", "file_id": "b", "caption": "c2"},
        ],
    )
    assert inserted1 == 2

    inserted2 = await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "video", "file_id": "c", "caption": None},
        ],
    )
    assert inserted2 == 1

    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert [p["file_id"] for p in posts] == ["a", "b", "c"]
    assert [int(p["position"]) for p in posts] == [0, 1, 2]

    # Delete the middle item and ensure positions compact.
    await db.delete_queued_post(int(posts[1]["id"]))
    posts2 = await db.get_queued_posts(schedule_id, limit=10)
    assert [p["file_id"] for p in posts2] == ["a", "c"]
    assert [int(p["position"]) for p in posts2] == [0, 1]


@pytest.mark.asyncio
async def test_add_queued_posts_bulk_persists_forward_metadata(initialized_db) -> None:
    user_id = 777
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-7777", channel_name="Forward")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Forward Schedule",
        pattern={"type": "interval", "minutes": 60},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {
                "media_type": "photo",
                "file_id": "a",
                "caption": "c",
                "forward_from_chat_id": 123,
                "forward_from_message_id": 456,
                "forward_origin_chat_id": -1001234567890,
                "forward_origin_message_id": 207,
            }
        ],
    )

    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert len(posts) == 1
    assert int(posts[0]["forward_from_chat_id"]) == 123
    assert int(posts[0]["forward_from_message_id"]) == 456
    assert int(posts[0]["forward_origin_chat_id"]) == -1001234567890
    assert int(posts[0]["forward_origin_message_id"]) == 207


@pytest.mark.asyncio
async def test_forward_origin_allowlist_roundtrip(initialized_db) -> None:
    user_id = 888
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    assert await db.get_forward_origin_allowlist(user_id) == []

    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-1001)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-1002)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-1002)  # idempotent

    assert await db.get_forward_origin_allowlist(user_id) == [-1002, -1001]

    await db.remove_forward_origin_allowlist(user_id=user_id, origin_chat_id=-1001)
    assert await db.get_forward_origin_allowlist(user_id) == [-1002]

    await db.clear_forward_origin_allowlist(user_id)
    assert await db.get_forward_origin_allowlist(user_id) == []


@pytest.mark.asyncio
async def test_scheduled_for_helpers_and_earliest(initialized_db) -> None:
    user_id = 456
    await db.upsert_user(user_id=user_id, username="u2", first_name="f2", last_name="l2", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-2002", channel_name="Channel 2")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Schedule 2",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "p1"},
            {"media_type": "photo", "file_id": "p2"},
            {"media_type": "photo", "file_id": "p3"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert len(posts) == 3

    base = datetime.now(timezone.utc).replace(microsecond=0)
    t1 = base + timedelta(seconds=30)
    t2 = base + timedelta(seconds=10)
    t3 = base + timedelta(seconds=20)

    await db.bulk_update_posts_scheduled_for(
        [
            (int(posts[0]["id"]), t1),
            (int(posts[1]["id"]), t2),
            (int(posts[2]["id"]), t3),
        ]
    )

    earliest_raw = await db.get_earliest_scheduled_for()
    earliest = _parse_timestamp(earliest_raw)
    assert earliest is not None
    assert earliest.replace(microsecond=0) == t2

    # Unscheduled query should now return empty (all three have scheduled_for).
    unscheduled = await db.get_queued_posts_unscheduled(schedule_id, limit=10)
    assert unscheduled == []


@pytest.mark.asyncio
async def test_active_users_and_delivery_stats_daily(initialized_db) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=120)

    # Create two users; only one is active since 90 days.
    await db.upsert_user(user_id=1, username="u1", first_name="f", last_name="l", is_admin=False)
    await db.upsert_user(user_id=2, username="u2", first_name="f", last_name="l", is_admin=False)

    # Force user 2 to look inactive by pushing last_active_at back.
    async with transaction() as conn:
        await conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (to_sqlite_timestamp(old), 2))

    active = await db.get_active_users(since=now - timedelta(days=90))
    assert [int(u["id"]) for u in active] == [1]

    active_count = await db.get_active_user_count(since=now - timedelta(days=90))
    assert active_count == 1

    # Delivery stats: today increments and sums.
    today = now.date()
    await db.increment_delivery_stats_daily(day=today, posts_sent_delta=2, send_failures_delta=1)
    await db.increment_delivery_stats_daily(day=today, posts_sent_delta=1, send_failures_delta=0)

    summed = await db.get_delivery_stats_sum_since(since_day=today)
    assert summed["posts_sent"] == 3
    assert summed["send_failures"] == 1


@pytest.mark.asyncio
async def test_user_context_selection_roundtrip(initialized_db) -> None:
    user_id = 999
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-9009", channel_name="C")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
        state="paused",
    )

    # Select channel only
    await db.set_user_context(user_id=user_id, selected_channel_id=int(channel["id"]), selected_schedule_id=None)
    ctx = await db.get_user_context(user_id)
    assert ctx["selected_channel_id"] == int(channel["id"])
    assert ctx["selected_schedule_id"] is None

    details = await db.get_user_context_details(user_id)
    assert details["telegram_channel_id"] == "-9009"
    assert details["channel_name"] == "C"
    assert details["selected_schedule_id"] is None

    # Select schedule
    await db.set_user_context(
        user_id=user_id,
        selected_channel_id=int(channel["id"]),
        selected_schedule_id=int(schedule["id"]),
    )
    details2 = await db.get_user_context_details(user_id)
    assert int(details2["selected_schedule_id"]) == int(schedule["id"])
    assert details2["schedule_name"] == "S"

    # Clear
    await db.clear_user_context(user_id)
    ctx2 = await db.get_user_context(user_id)
    assert ctx2["selected_channel_id"] is None
    assert ctx2["selected_schedule_id"] is None

