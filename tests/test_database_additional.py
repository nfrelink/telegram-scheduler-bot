"""Tests for previously uncovered database query functions."""
from __future__ import annotations

import pytest

from database import queries as db


async def _make_schedule(user_id: int, *, suffix: str = "") -> tuple[dict, dict, dict]:
    """Helper: create user + channel + schedule, return all three."""
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-100{user_id}{suffix}",
        channel_name=f"Chan {user_id}{suffix}",
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name=f"Sched {user_id}{suffix}",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="paused",
    )
    return channel, schedule, {}


# ---------------------------------------------------------------------------
# get_user_timezone / set_user_timezone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_user_timezone_returns_none_when_not_set(initialized_db) -> None:
    user_id = 8001
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    assert await db.get_user_timezone(user_id) is None


@pytest.mark.asyncio
async def test_set_and_get_user_timezone_roundtrip(initialized_db) -> None:
    user_id = 8002
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    await db.set_user_timezone(user_id, "Asia/Tokyo")
    assert await db.get_user_timezone(user_id) == "Asia/Tokyo"

    await db.set_user_timezone(user_id, "America/New_York")
    assert await db.get_user_timezone(user_id) == "America/New_York"


@pytest.mark.asyncio
async def test_set_user_timezone_none_clears_value(initialized_db) -> None:
    user_id = 8003
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    await db.set_user_timezone(user_id, "UTC")
    assert await db.get_user_timezone(user_id) == "UTC"

    await db.set_user_timezone(user_id, None)
    assert await db.get_user_timezone(user_id) is None


# ---------------------------------------------------------------------------
# get_user_channels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_user_channels_returns_all_channels_for_user(initialized_db) -> None:
    user_id = 8004
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    ch1 = await db.create_channel(user_id=user_id, telegram_channel_id="-80041", channel_name="Channel A")
    ch2 = await db.create_channel(user_id=user_id, telegram_channel_id="-80042", channel_name="Channel B")

    channels = await db.get_user_channels(user_id)
    ids = {int(ch["id"]) for ch in channels}
    assert int(ch1["id"]) in ids
    assert int(ch2["id"]) in ids
    assert len(channels) == 2


@pytest.mark.asyncio
async def test_get_user_channels_only_returns_own_channels(initialized_db) -> None:
    await db.upsert_user(user_id=8005, username="u", first_name="f", last_name="l", is_admin=False)
    await db.upsert_user(user_id=8006, username="u", first_name="f", last_name="l", is_admin=False)
    await db.create_channel(user_id=8005, telegram_channel_id="-80051", channel_name="User A channel")
    await db.create_channel(user_id=8006, telegram_channel_id="-80061", channel_name="User B channel")

    channels_a = await db.get_user_channels(8005)
    assert all(int(ch["user_id"]) == 8005 for ch in channels_a)
    assert len(channels_a) == 1


# ---------------------------------------------------------------------------
# get_channel_by_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_channel_by_id_returns_correct_data(initialized_db) -> None:
    user_id = 8007
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-8007", channel_name="Named Channel")

    fetched = await db.get_channel_by_id(int(ch["id"]))
    assert fetched is not None
    assert fetched["channel_name"] == "Named Channel"


@pytest.mark.asyncio
async def test_get_channel_by_id_returns_none_for_missing_id(initialized_db) -> None:
    assert await db.get_channel_by_id(99999999) is None


# ---------------------------------------------------------------------------
# get_queue_count / get_channel_queue_count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_queue_count_starts_at_zero(initialized_db) -> None:
    user_id = 8008
    ch, schedule, _ = await _make_schedule(user_id)
    assert await db.get_queue_count(int(schedule["id"])) == 0


@pytest.mark.asyncio
async def test_get_queue_count_reflects_bulk_insert(initialized_db) -> None:
    user_id = 8009
    ch, schedule, _ = await _make_schedule(user_id)
    schedule_id = int(schedule["id"])

    await db.add_queued_posts_bulk(
        schedule_id,
        [{"media_type": "photo", "file_id": "a"}, {"media_type": "photo", "file_id": "b"}],
    )
    assert await db.get_queue_count(schedule_id) == 2


@pytest.mark.asyncio
async def test_get_channel_queue_count_aggregates_across_schedules(initialized_db) -> None:
    user_id = 8010
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-8010", channel_name="C")
    channel_id = int(ch["id"])

    s1 = await db.create_schedule(
        channel_db_id=channel_id, name="S1",
        pattern={"type": "interval", "hours": 1}, timezone_name="UTC", state="paused",
    )
    s2 = await db.create_schedule(
        channel_db_id=channel_id, name="S2",
        pattern={"type": "interval", "hours": 2}, timezone_name="UTC", state="paused",
    )

    assert await db.get_channel_queue_count(channel_id) == 0

    await db.add_queued_posts_bulk(int(s1["id"]), [{"media_type": "photo", "file_id": "x"}])
    await db.add_queued_posts_bulk(int(s2["id"]), [{"media_type": "photo", "file_id": "y"}, {"media_type": "photo", "file_id": "z"}])

    assert await db.get_channel_queue_count(channel_id) == 3


# ---------------------------------------------------------------------------
# get_queued_post_with_owner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_queued_post_with_owner_returns_correct_owner(initialized_db) -> None:
    user_id = 8011
    ch, schedule, _ = await _make_schedule(user_id)
    await db.add_queued_posts_bulk(int(schedule["id"]), [{"media_type": "photo", "file_id": "z"}])

    posts = await db.get_queued_posts(int(schedule["id"]), limit=1)
    post_id = int(posts[0]["id"])

    row = await db.get_queued_post_with_owner(post_id)
    assert row is not None
    assert int(row["owner_user_id"]) == user_id


@pytest.mark.asyncio
async def test_get_queued_post_with_owner_returns_none_for_missing_id(initialized_db) -> None:
    assert await db.get_queued_post_with_owner(99999999) is None


# ---------------------------------------------------------------------------
# get_schedule_with_channel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_schedule_with_channel_returns_joined_data(initialized_db) -> None:
    user_id = 8012
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    ch = await db.create_channel(user_id=user_id, telegram_channel_id="-8012", channel_name="Chan 8012")
    schedule = await db.create_schedule(
        channel_db_id=int(ch["id"]),
        name="Sched 8012",
        pattern={"type": "interval", "hours": 2},
        timezone_name="UTC",
        state="paused",
    )

    result = await db.get_schedule_with_channel(int(schedule["id"]))
    assert result is not None
    assert result["name"] == "Sched 8012"
    assert result["channel_name"] == "Chan 8012"
    assert result["telegram_channel_id"] == "-8012"
    assert int(result["owner_user_id"]) == user_id


@pytest.mark.asyncio
async def test_get_schedule_with_channel_returns_none_for_missing_id(initialized_db) -> None:
    assert await db.get_schedule_with_channel(99999999) is None


# ---------------------------------------------------------------------------
# delete_schedule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_schedule_removes_it_from_db(initialized_db) -> None:
    user_id = 8013
    ch, schedule, _ = await _make_schedule(user_id)
    schedule_id = int(schedule["id"])

    assert await db.get_schedule(schedule_id) is not None
    await db.delete_schedule(schedule_id, user_id=user_id)
    assert await db.get_schedule(schedule_id) is None


@pytest.mark.asyncio
async def test_delete_schedule_is_idempotent(initialized_db) -> None:
    user_id = 8014
    ch, schedule, _ = await _make_schedule(user_id)
    schedule_id = int(schedule["id"])

    await db.delete_schedule(schedule_id, user_id=user_id)
    # Second delete should not raise.
    await db.delete_schedule(schedule_id, user_id=user_id)
    assert await db.get_schedule(schedule_id) is None
