from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import transaction
from database import queries as db
from database.time import parse_timestamp, to_sqlite_timestamp


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
    earliest = parse_timestamp(earliest_raw)
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


# --- Bulk staging ---


@pytest.mark.asyncio
async def test_bulk_staging_session_lifecycle(initialized_db) -> None:
    user_id = 600
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

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
async def test_bulk_staging_items_lifecycle(initialized_db) -> None:
    user_id = 601
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

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
async def test_fingerprint_insert_and_query_by_file_unique_id(initialized_db) -> None:
    user_id = 700
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-7001", channel_name="FP Channel")
    ch_id = int(channel["id"])

    ids = await db.add_fingerprints_bulk(ch_id, [
        {"file_unique_id": "uniq1", "dhash": "12345", "file_id": "fid1", "media_type": "photo", "queued_post_id": None},
        {"file_unique_id": "uniq2", "dhash": None, "file_id": "fid2", "media_type": "video", "queued_post_id": None},
    ])
    assert len(ids) == 2

    match = await db.find_fingerprint_by_file_unique_id(ch_id, "uniq1")
    assert match is not None
    assert match["file_unique_id"] == "uniq1"
    assert match["dhash"] == "12345"

    no_match = await db.find_fingerprint_by_file_unique_id(ch_id, "nonexistent")
    assert no_match is None


@pytest.mark.asyncio
async def test_get_channel_dhashes(initialized_db) -> None:
    user_id = 701
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-7002", channel_name="Hash Channel")
    ch_id = int(channel["id"])

    await db.add_fingerprints_bulk(ch_id, [
        {"file_unique_id": "u1", "dhash": "100", "file_id": "f1", "media_type": "photo", "queued_post_id": None},
        {"file_unique_id": "u2", "dhash": None, "file_id": "f2", "media_type": "video", "queued_post_id": None},
        {"file_unique_id": "u3", "dhash": "200", "file_id": "f3", "media_type": "photo", "queued_post_id": None},
    ])

    hashes = await db.get_channel_dhashes(ch_id)
    assert len(hashes) == 2
    dhash_values = {h for _, h in hashes}
    assert dhash_values == {100, 200}


@pytest.mark.asyncio
async def test_mark_fingerprint_posted_and_delete_unposted(initialized_db) -> None:
    user_id = 702
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-7003", channel_name="Post Channel")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]), name="S", pattern={"type": "interval", "hours": 1},
    )
    ch_id = int(channel["id"])
    count, post_ids = await db.add_queued_posts_bulk(int(schedule["id"]), [
        {"media_type": "photo", "file_id": "pf1"},
        {"media_type": "photo", "file_id": "pf2"},
    ])

    await db.add_fingerprints_bulk(ch_id, [
        {"file_unique_id": "pu1", "dhash": "999", "file_id": "pf1", "media_type": "photo", "queued_post_id": post_ids[0]},
        {"file_unique_id": "pu2", "dhash": "888", "file_id": "pf2", "media_type": "photo", "queued_post_id": post_ids[1]},
    ])

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
async def test_duplicate_detection_settings_toggle(initialized_db) -> None:
    user_id = 703
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id="-7004", channel_name="Settings Channel")
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
async def test_bulk_staging_items_preserve_all_fields(initialized_db) -> None:
    user_id = 602
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

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


# --- Atomic orchestrators --------------------------------------------------
#
# These verify the Phase 1.2 invariants: each multi-step write executes inside
# a single transaction so partial failures do not leave the DB inconsistent.


async def _seed_user_channel_schedule(user_id: int, *, tg_id: str = "-1099"):
    """Helper: create user/channel/schedule and return (channel_id, schedule_id)."""
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id=tg_id, channel_name="C")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="active",
    )
    return int(channel["id"]), int(schedule["id"])


async def _today_stats() -> dict[str, int]:
    return await db.get_delivery_stats_sum_since(since_day=datetime.now(timezone.utc).date())


@pytest.mark.asyncio
async def test_complete_post_send_applies_all_writes(initialized_db) -> None:
    user_id = 9001
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9001")
    _, post_ids = await db.add_queued_posts_bulk(
        sid,
        [
            {"media_type": "photo", "file_id": "x1"},
            {"media_type": "photo", "file_id": "x2"},
        ],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "u1", "dhash": "1", "file_id": "x1", "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )
    stats_before = await _today_stats()
    sched_before = await db.get_schedule(sid)
    assert sched_before["last_run_at"] is None
    assert sched_before["next_planned_run_at"] is None

    next_planned = datetime.now(timezone.utc) + timedelta(minutes=30)
    await db.complete_post_send(
        post_id=post_ids[0],
        schedule_id=sid,
        owner_user_id=user_id,
        day=datetime.now(timezone.utc).date(),
        next_planned_run_at=next_planned,
    )

    stats_after = await _today_stats()
    assert stats_after["posts_sent"] == stats_before["posts_sent"] + 1

    fp = await db.find_fingerprint_by_file_unique_id(ch_id, "u1")
    assert fp is not None and fp["posted_at"] is not None

    remaining = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in remaining] == ["x2"]
    assert int(remaining[0]["position"]) == 0  # FIFO compacted

    sched_after = await db.get_schedule(sid)
    assert sched_after["last_run_at"] is not None
    # NPR was advanced atomically alongside the rest.
    from database.time import parse_timestamp as _pt
    assert _pt(sched_after["next_planned_run_at"]) is not None
    # Compare at second precision (SQLite stores TEXT seconds).
    assert _pt(sched_after["next_planned_run_at"]).replace(microsecond=0) == \
        next_planned.replace(microsecond=0)


@pytest.mark.asyncio
async def test_complete_post_send_rolls_back_on_inner_failure(initialized_db, monkeypatch) -> None:
    """If any in-tx step raises, all preceding writes in the orchestrator must
    be rolled back. We force the last step (update_schedule_next_planned_run)
    to raise and assert that earlier steps left no trace."""
    user_id = 9002
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9002")
    _, post_ids = await db.add_queued_posts_bulk(
        sid,
        [{"media_type": "photo", "file_id": "y1"}],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "v1", "dhash": "1", "file_id": "y1", "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )
    stats_before = await _today_stats()

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    # Force the *last* in-tx step to raise so all preceding writes (stats,
    # mark posted, delete, last_run_at bump) must roll back.
    monkeypatch.setattr(db, "_update_schedule_next_planned_run_in_tx", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await db.complete_post_send(
            post_id=post_ids[0],
            schedule_id=sid,
            owner_user_id=user_id,
            day=datetime.now(timezone.utc).date(),
            next_planned_run_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )

    stats_after = await _today_stats()
    assert stats_after == stats_before  # increment rolled back

    fp = await db.find_fingerprint_by_file_unique_id(ch_id, "v1")
    assert fp is not None and fp["posted_at"] is None  # mark_posted rolled back

    remaining = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in remaining] == ["y1"]  # delete rolled back

    sched = await db.get_schedule(sid)
    assert sched["last_run_at"] is None  # last_run_at bump rolled back
    assert sched["next_planned_run_at"] is None  # NPR write itself never landed


@pytest.mark.asyncio
async def test_complete_post_retry_increments_failures_and_updates_post(initialized_db) -> None:
    user_id = 9003
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-9003")
    _, post_ids = await db.add_queued_posts_bulk(
        sid,
        [{"media_type": "photo", "file_id": "z1"}],
    )
    stats_before = await _today_stats()
    retry_time = datetime.now(timezone.utc) + timedelta(minutes=4)

    await db.complete_post_retry(
        post_id=post_ids[0], retry_count=2, scheduled_for=retry_time, day=datetime.now(timezone.utc).date()
    )

    stats_after = await _today_stats()
    assert stats_after["send_failures"] == stats_before["send_failures"] + 1

    posts = await db.get_queued_posts(sid, limit=10)
    assert int(posts[0]["retry_count"]) == 2
    assert parse_timestamp(posts[0]["scheduled_for"]) is not None


@pytest.mark.asyncio
async def test_complete_post_failure_pause_pauses_schedule(initialized_db) -> None:
    user_id = 9004
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-9004")
    stats_before = await _today_stats()

    await db.complete_post_failure_pause(
        schedule_id=sid, owner_user_id=user_id, day=datetime.now(timezone.utc).date()
    )

    stats_after = await _today_stats()
    assert stats_after["send_failures"] == stats_before["send_failures"] + 1

    sched = await db.get_schedule(sid)
    assert sched["state"] == "paused"


@pytest.mark.asyncio
async def test_cancel_queued_post_removes_post_and_unposted_fingerprints(initialized_db) -> None:
    user_id = 9005
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9005")
    _, post_ids = await db.add_queued_posts_bulk(
        sid,
        [
            {"media_type": "photo", "file_id": "a"},
            {"media_type": "photo", "file_id": "b"},
        ],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "ua", "dhash": "1", "file_id": "a", "media_type": "photo", "queued_post_id": post_ids[0]},
            {"file_unique_id": "ub", "dhash": "2", "file_id": "b", "media_type": "photo", "queued_post_id": post_ids[1]},
        ],
    )

    await db.cancel_queued_post(post_id=post_ids[0], user_id=user_id)

    posts = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in posts] == ["b"]
    assert int(posts[0]["position"]) == 0  # FIFO compacted

    assert await db.find_fingerprint_by_file_unique_id(ch_id, "ua") is None
    assert await db.find_fingerprint_by_file_unique_id(ch_id, "ub") is not None


@pytest.mark.asyncio
async def test_cancel_queued_post_rolls_back_fingerprint_delete_on_failure(
    initialized_db, monkeypatch
) -> None:
    """If the queued-post delete step raises, the fingerprint deletion executed
    earlier in the same transaction must be rolled back."""
    user_id = 9006
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9006")
    _, post_ids = await db.add_queued_posts_bulk(
        sid,
        [{"media_type": "photo", "file_id": "k"}],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "uk", "dhash": "1", "file_id": "k", "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(db, "_delete_queued_post_in_tx", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await db.cancel_queued_post(post_id=post_ids[0], user_id=user_id)

    fp = await db.find_fingerprint_by_file_unique_id(ch_id, "uk")
    assert fp is not None  # rolled back
    posts = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in posts] == ["k"]

