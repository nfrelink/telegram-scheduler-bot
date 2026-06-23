from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database import queries as db
from database import transaction
from database.connection import get_db
from database.time import parse_timestamp, to_sqlite_timestamp


@pytest.mark.asyncio
async def test_add_queued_posts_bulk_appends_and_compacts_positions(
    _initialized_db,
) -> None:
    user_id = 123
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-1001", channel_name="Test Channel"
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Test Schedule",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    count1, ids1 = await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "a", "caption": "c1"},
            {"media_type": "photo", "file_id": "b", "caption": "c2"},
        ],
    )
    assert count1 == 2
    assert len(ids1) == 2

    count2, ids2 = await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "video", "file_id": "c", "caption": None},
        ],
    )
    assert count2 == 1
    assert len(ids2) == 1

    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert [p["file_id"] for p in posts] == ["a", "b", "c"]
    assert [int(p["position"]) for p in posts] == [0, 1, 2]

    # Delete the middle item and ensure positions compact.
    await db.delete_queued_post(int(posts[1]["id"]), user_id=user_id)
    posts2 = await db.get_queued_posts(schedule_id, limit=10)
    assert [p["file_id"] for p in posts2] == ["a", "c"]
    assert [int(p["position"]) for p in posts2] == [0, 1]


@pytest.mark.asyncio
async def test_add_queued_posts_bulk_persists_forward_metadata(_initialized_db) -> None:
    user_id = 777
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-7777", channel_name="Forward"
    )
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
async def test_forward_origin_allowlist_roundtrip(_initialized_db) -> None:
    user_id = 888
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )

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
async def test_scheduled_for_helpers_and_earliest(_initialized_db) -> None:
    user_id = 456
    await db.upsert_user(
        user_id=user_id, username="u2", first_name="f2", last_name="l2", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-2002", channel_name="Channel 2"
    )
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

    base = datetime.now(UTC).replace(microsecond=0)
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
    earliest = parse_timestamp(earliest_raw)
    assert earliest is not None
    assert earliest.replace(microsecond=0) == t2

    # Unscheduled query should now return empty (all three have scheduled_for).
    unscheduled = await db.get_queued_posts_unscheduled(schedule_id, limit=10)
    assert unscheduled == []


@pytest.mark.asyncio
async def test_active_users_and_delivery_stats_daily(_initialized_db) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - timedelta(days=120)

    # Create two users; only one is active since 90 days.
    await db.upsert_user(user_id=1, username="u1", first_name="f", last_name="l", is_admin=False)
    await db.upsert_user(user_id=2, username="u2", first_name="f", last_name="l", is_admin=False)

    # Force user 2 to look inactive by pushing last_active_at back.
    async with transaction() as conn:
        await conn.execute(
            "UPDATE users SET last_active_at = ? WHERE id = ?",
            (to_sqlite_timestamp(old), 2),
        )

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
async def test_user_context_selection_roundtrip(_initialized_db) -> None:
    user_id = 999
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )

    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-9009", channel_name="C"
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
        state="paused",
    )

    # Select channel only
    await db.set_user_context(
        user_id=user_id,
        selected_channel_id=int(channel["id"]),
        selected_schedule_id=None,
    )
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


# --- Bulk staging ---


@pytest.mark.asyncio
async def test_bulk_staging_session_lifecycle(_initialized_db) -> None:
    user_id = 600
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )

    assert await db.get_bulk_session(user_id) is None

    await db.create_bulk_session(user_id, schedule_id=1, caption_mode="remove")
    session = await db.get_bulk_session(user_id)
    assert session is not None
    assert session["caption_mode"] == "remove"
    assert session["single_caption"] is None

    await db.update_bulk_session_caption(user_id, caption_mode="single", single_caption="hello")
    session2 = await db.get_bulk_session(user_id)
    assert session2["caption_mode"] == "single"
    assert session2["single_caption"] == "hello"

    await db.clear_staging(user_id)
    assert await db.get_bulk_session(user_id) is None


@pytest.mark.asyncio
async def test_bulk_staging_items_lifecycle(_initialized_db) -> None:
    user_id = 601
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )

    assert await db.get_staging_count(user_id) == 0

    await db.add_staging_item(user_id, media_type="photo", file_id="fid1")
    await db.add_staging_item(user_id, media_type="video", file_id="fid2", media_group_id="mg1")
    await db.add_staging_item(user_id, media_type="photo", file_id="fid3", media_group_id="mg1")

    assert await db.get_staging_count(user_id) == 3

    items = await db.get_staging_items(user_id)
    assert len(items) == 3
    assert items[0]["file_id"] == "fid1"
    assert items[0]["media_group_id"] is None
    assert items[1]["media_group_id"] == "mg1"
    assert items[2]["media_group_id"] == "mg1"
    # Positions are sequential
    assert [items[i]["position"] for i in range(3)] == [0, 1, 2]

    await db.clear_staging(user_id)
    assert await db.get_staging_count(user_id) == 0


# --- Media fingerprints ---


@pytest.mark.asyncio
async def test_fingerprint_insert_and_query_by_file_unique_id(_initialized_db) -> None:
    user_id = 700
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-7001", channel_name="FP Channel"
    )
    ch_id = int(channel["id"])

    ids = await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "uniq1",
                "dhash": "12345",
                "file_id": "fid1",
                "media_type": "photo",
                "queued_post_id": None,
            },
            {
                "file_unique_id": "uniq2",
                "dhash": None,
                "file_id": "fid2",
                "media_type": "video",
                "queued_post_id": None,
            },
        ],
    )
    assert len(ids) == 2

    match = await db.find_fingerprint_by_file_unique_id(ch_id, "uniq1")
    assert match is not None
    assert match["file_unique_id"] == "uniq1"
    assert match["dhash"] == "12345"

    no_match = await db.find_fingerprint_by_file_unique_id(ch_id, "nonexistent")
    assert no_match is None


@pytest.mark.asyncio
async def test_get_channel_dhashes(_initialized_db) -> None:
    user_id = 701
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-7002", channel_name="Hash Channel"
    )
    ch_id = int(channel["id"])

    await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "u1",
                "dhash": "100",
                "file_id": "f1",
                "media_type": "photo",
                "queued_post_id": None,
            },
            {
                "file_unique_id": "u2",
                "dhash": None,
                "file_id": "f2",
                "media_type": "video",
                "queued_post_id": None,
            },
            {
                "file_unique_id": "u3",
                "dhash": "200",
                "file_id": "f3",
                "media_type": "photo",
                "queued_post_id": None,
            },
        ],
    )

    hashes = await db.get_channel_dhashes(ch_id)
    assert len(hashes) == 2
    dhash_values = {h for _, h in hashes}
    assert dhash_values == {100, 200}


@pytest.mark.asyncio
async def test_mark_fingerprint_posted_and_delete_unposted(_initialized_db) -> None:
    user_id = 702
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-7003", channel_name="Post Channel"
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "hours": 1},
    )
    ch_id = int(channel["id"])
    _count, post_ids = await db.add_queued_posts_bulk(
        int(schedule["id"]),
        [
            {"media_type": "photo", "file_id": "pf1"},
            {"media_type": "photo", "file_id": "pf2"},
        ],
    )

    await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "pu1",
                "dhash": "999",
                "file_id": "pf1",
                "media_type": "photo",
                "queued_post_id": post_ids[0],
            },
            {
                "file_unique_id": "pu2",
                "dhash": "888",
                "file_id": "pf2",
                "media_type": "photo",
                "queued_post_id": post_ids[1],
            },
        ],
    )

    # Mark first as posted
    await db.mark_fingerprint_posted(post_ids[0])
    fp1 = await db.find_fingerprint_by_file_unique_id(ch_id, "pu1")
    assert fp1 is not None
    assert fp1["posted_at"] is not None

    # Delete unposted fingerprints for second post — should be removed
    await db.delete_unposted_fingerprints(post_ids[1])
    fp2 = await db.find_fingerprint_by_file_unique_id(ch_id, "pu2")
    assert fp2 is None

    # First fingerprint should still exist (it was posted)
    fp1_check = await db.find_fingerprint_by_file_unique_id(ch_id, "pu1")
    assert fp1_check is not None


@pytest.mark.asyncio
async def test_duplicate_detection_settings_toggle(_initialized_db) -> None:
    user_id = 703
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id="-7004", channel_name="Settings Channel"
    )
    ch_id = int(channel["id"])

    assert await db.get_channel_duplicate_detection(ch_id) is False
    await db.set_channel_duplicate_detection(ch_id, enabled=True)
    assert await db.get_channel_duplicate_detection(ch_id) is True
    await db.set_channel_duplicate_detection(ch_id, enabled=False)
    assert await db.get_channel_duplicate_detection(ch_id) is False

    assert await db.get_user_duplicate_alerts(user_id) is True
    await db.set_user_duplicate_alerts(user_id, enabled=False)
    assert await db.get_user_duplicate_alerts(user_id) is False
    await db.set_user_duplicate_alerts(user_id, enabled=True)
    assert await db.get_user_duplicate_alerts(user_id) is True


@pytest.mark.asyncio
async def test_bulk_staging_items_preserve_all_fields(_initialized_db) -> None:
    user_id = 602
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )

    await db.add_staging_item(
        user_id,
        media_type="photo",
        file_id="fwd_fid",
        caption="cap",
        caption_entities='[{"type": "bold"}]',
        media_group_id="mg2",
        forward_from_chat_id=111,
        forward_from_message_id=222,
        forward_origin_chat_id=333,
        forward_origin_message_id=444,
        raw_origin_chat_id=555,
        raw_origin_message_id=666,
        raw_origin_is_forwarded=True,
    )

    items = await db.get_staging_items(user_id)
    assert len(items) == 1
    item = items[0]
    assert item["caption"] == "cap"
    assert item["caption_entities"] == '[{"type": "bold"}]'
    assert item["forward_from_chat_id"] == 111
    assert item["forward_from_message_id"] == 222
    assert item["forward_origin_chat_id"] == 333
    assert item["forward_origin_message_id"] == 444
    assert item["raw_origin_chat_id"] == 555
    assert item["raw_origin_message_id"] == 666
    assert item["raw_origin_is_forwarded"] == 1  # SQLite stores booleans as ints


# ---------------------------------------------------------------------------
# count_queued_posts / get_latest_active_schedule_run_at
# (added with admin notifications + heartbeat in Phase 2.2)
# ---------------------------------------------------------------------------


async def _mk_schedule_for_count(user_id: int, suffix: str, *, state: str = "active") -> int:
    """Smallest path to a real schedule row; returns its id."""
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
        pattern={"type": "interval", "minutes": 30},
        timezone_name="UTC",
        state=state,
    )
    return int(schedule["id"])


@pytest.mark.asyncio
async def test_count_queued_posts_returns_zero_when_empty(_initialized_db) -> None:
    sid = await _mk_schedule_for_count(9001, "9001")
    assert await db.count_queued_posts(sid) == 0


@pytest.mark.asyncio
async def test_count_queued_posts_counts_existing_rows(_initialized_db) -> None:
    sid = await _mk_schedule_for_count(9002, "9002")
    await db.add_queued_posts_bulk(
        sid, [{"media_type": "photo", "file_id": f"f{i}"} for i in range(5)]
    )
    assert await db.count_queued_posts(sid) == 5


@pytest.mark.asyncio
async def test_get_latest_active_schedule_run_at_with_no_active_schedules(
    _initialized_db,
) -> None:
    """Paused schedules are explicitly excluded — only active ones count
    against the heartbeat timer."""
    sid = await _mk_schedule_for_count(9003, "9003", state="paused")
    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?",
            ("2026-04-20 10:00:00", sid),
        )
        await conn.commit()
    assert await db.get_latest_active_schedule_run_at() is None


@pytest.mark.asyncio
async def test_get_latest_active_schedule_run_at_returns_max_across_active(
    _initialized_db,
) -> None:
    sid_a = await _mk_schedule_for_count(9004, "9004a")
    sid_b = await _mk_schedule_for_count(9004, "9004b")
    sid_c = await _mk_schedule_for_count(9004, "9004c", state="paused")

    async with get_db() as conn:
        await conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?",
            ("2026-04-20 10:00:00", sid_a),
        )
        await conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?",
            ("2026-04-20 12:00:00", sid_b),
        )
        # The paused row has the latest timestamp but must be ignored.
        await conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?",
            ("2026-04-20 23:00:00", sid_c),
        )
        await conn.commit()

    latest = await db.get_latest_active_schedule_run_at()
    assert latest is not None
    assert latest == datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
