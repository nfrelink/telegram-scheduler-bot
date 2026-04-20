"""Tests for services.posting — orchestrator atomicity + thin wrappers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import queries as db
from database.time import parse_timestamp
from services import posting


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

async def _seed_user_channel_schedule(user_id: int, *, tg_id: str = "-1099"):
    """Helper: create user/channel/schedule and return (channel_id, schedule_id)."""
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    channel = await db.create_channel(
        user_id=user_id, telegram_channel_id=tg_id, channel_name="C"
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="active",
    )
    return int(channel["id"]), int(schedule["id"])


async def _today_stats() -> dict[str, int]:
    return await db.get_delivery_stats_sum_since(
        since_day=datetime.now(timezone.utc).date()
    )


# ---------------------------------------------------------------------------
# enqueue / enqueue_bulk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_appends_to_fifo(initialized_db) -> None:
    user_id = 8001
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-8001")
    await posting.enqueue(schedule_id=sid, media_type="photo", file_id="e1")
    await posting.enqueue(schedule_id=sid, media_type="photo", file_id="e2")
    posts = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in posts] == ["e1", "e2"]
    assert [int(p["position"]) for p in posts] == [0, 1]


@pytest.mark.asyncio
async def test_enqueue_bulk_links_fingerprints_by_file_id(initialized_db) -> None:
    """enqueue_bulk must stamp queued_post_id onto fingerprints whose file_id
    matches an inserted post; mismatched / missing file_ids stay None."""
    user_id = 8002
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-8002")
    posts = [
        {"media_type": "photo", "file_id": "p1"},
        {"media_type": "photo", "file_id": "p2"},
        {"media_type": "photo"},  # native forward, no file_id
    ]
    fingerprints = [
        {"file_unique_id": "u-p1", "dhash": None, "file_id": "p1", "media_type": "photo"},
        {"file_unique_id": "u-p2", "dhash": None, "file_id": "p2", "media_type": "photo"},
        {"file_unique_id": "u-orphan", "dhash": None, "file_id": "no-such", "media_type": "photo"},
    ]
    inserted, post_ids = await posting.enqueue_bulk(
        sid, posts=posts, fingerprints=fingerprints, channel_db_id=ch_id
    )
    assert inserted == 3
    assert len(post_ids) == 3

    fp_p1 = await db.find_fingerprint_by_file_unique_id(ch_id, "u-p1")
    fp_p2 = await db.find_fingerprint_by_file_unique_id(ch_id, "u-p2")
    fp_orphan = await db.find_fingerprint_by_file_unique_id(ch_id, "u-orphan")
    assert fp_p1 is not None and int(fp_p1["queued_post_id"]) == post_ids[0]
    assert fp_p2 is not None and int(fp_p2["queued_post_id"]) == post_ids[1]
    assert fp_orphan is not None and fp_orphan["queued_post_id"] is None


@pytest.mark.asyncio
async def test_enqueue_bulk_skips_fingerprints_when_none(initialized_db) -> None:
    """When fingerprints is None or channel_db_id is None we must still insert
    posts but not touch the fingerprints table."""
    user_id = 8003
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-8003")
    inserted, _ = await posting.enqueue_bulk(
        sid,
        posts=[{"media_type": "photo", "file_id": "z"}],
        fingerprints=None,
        channel_db_id=None,
    )
    assert inserted == 1


# ---------------------------------------------------------------------------
# pin / unpin / set_scheduled_for
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_and_unpin_roundtrip(initialized_db) -> None:
    user_id = 8004
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-8004")
    _, ids = await db.add_queued_posts_bulk(sid, [{"media_type": "photo", "file_id": "k"}])
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    await posting.pin(ids[0], pinned_at=when, user_id=user_id)
    post = (await db.get_queued_posts(sid, limit=1))[0]
    assert parse_timestamp(post["pinned_at"]) is not None
    await posting.unpin(ids[0], user_id=user_id)
    post = (await db.get_queued_posts(sid, limit=1))[0]
    assert post["pinned_at"] is None


@pytest.mark.asyncio
async def test_set_scheduled_for_and_bulk(initialized_db) -> None:
    user_id = 8005
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-8005")
    _, ids = await db.add_queued_posts_bulk(
        sid, [{"media_type": "photo", "file_id": "a"}, {"media_type": "photo", "file_id": "b"}]
    )
    base = datetime.now(timezone.utc) + timedelta(seconds=30)
    await posting.set_scheduled_for(ids[0], scheduled_for=base)
    await posting.bulk_set_scheduled_for([(ids[1], base + timedelta(seconds=15))])
    posts = await db.get_queued_posts(sid, limit=10)
    assert all(parse_timestamp(p["scheduled_for"]) is not None for p in posts)


# ---------------------------------------------------------------------------
# Atomic send-completion orchestrators
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_send_applies_all_writes(initialized_db) -> None:
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
            {"file_unique_id": "u1", "dhash": "1", "file_id": "x1",
             "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )
    stats_before = await _today_stats()
    sched_before = await db.get_schedule(sid)
    assert sched_before["last_run_at"] is None
    assert sched_before["next_planned_run_at"] is None

    next_planned = datetime.now(timezone.utc) + timedelta(minutes=30)
    await posting.complete_send(
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
    npr = parse_timestamp(sched_after["next_planned_run_at"])
    assert npr is not None
    # SQLite stores TEXT to second precision.
    assert npr.replace(microsecond=0) == next_planned.replace(microsecond=0)


@pytest.mark.asyncio
async def test_complete_send_rolls_back_on_inner_failure(
    initialized_db, monkeypatch
) -> None:
    """If any in-tx step raises, all preceding writes in the orchestrator
    must be rolled back. We force the last step
    (update_schedule_next_planned_run) to raise and assert that earlier steps
    left no trace."""
    user_id = 9002
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9002")
    _, post_ids = await db.add_queued_posts_bulk(
        sid, [{"media_type": "photo", "file_id": "y1"}],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "v1", "dhash": "1", "file_id": "y1",
             "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )
    stats_before = await _today_stats()

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(db, "_update_schedule_next_planned_run_in_tx", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await posting.complete_send(
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
async def test_complete_retry_increments_failures_and_updates_post(
    initialized_db,
) -> None:
    user_id = 9003
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-9003")
    _, post_ids = await db.add_queued_posts_bulk(
        sid, [{"media_type": "photo", "file_id": "z1"}],
    )
    stats_before = await _today_stats()
    retry_time = datetime.now(timezone.utc) + timedelta(minutes=4)

    await posting.complete_retry(
        post_id=post_ids[0],
        retry_count=2,
        scheduled_for=retry_time,
        day=datetime.now(timezone.utc).date(),
    )

    stats_after = await _today_stats()
    assert stats_after["send_failures"] == stats_before["send_failures"] + 1

    posts = await db.get_queued_posts(sid, limit=10)
    assert int(posts[0]["retry_count"]) == 2
    assert parse_timestamp(posts[0]["scheduled_for"]) is not None


@pytest.mark.asyncio
async def test_complete_failure_pause_pauses_schedule(initialized_db) -> None:
    user_id = 9004
    _, sid = await _seed_user_channel_schedule(user_id, tg_id="-9004")
    stats_before = await _today_stats()

    await posting.complete_failure_pause(
        schedule_id=sid,
        owner_user_id=user_id,
        day=datetime.now(timezone.utc).date(),
    )

    stats_after = await _today_stats()
    assert stats_after["send_failures"] == stats_before["send_failures"] + 1

    sched = await db.get_schedule(sid)
    assert sched["state"] == "paused"


@pytest.mark.asyncio
async def test_cancel_removes_post_and_unposted_fingerprints(initialized_db) -> None:
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
            {"file_unique_id": "ua", "dhash": "1", "file_id": "a",
             "media_type": "photo", "queued_post_id": post_ids[0]},
            {"file_unique_id": "ub", "dhash": "2", "file_id": "b",
             "media_type": "photo", "queued_post_id": post_ids[1]},
        ],
    )

    await posting.cancel(post_id=post_ids[0], user_id=user_id)

    posts = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in posts] == ["b"]
    assert int(posts[0]["position"]) == 0  # FIFO compacted

    assert await db.find_fingerprint_by_file_unique_id(ch_id, "ua") is None
    assert await db.find_fingerprint_by_file_unique_id(ch_id, "ub") is not None


@pytest.mark.asyncio
async def test_cancel_rolls_back_fingerprint_delete_on_failure(
    initialized_db, monkeypatch
) -> None:
    """If the queued-post delete step raises, the fingerprint deletion executed
    earlier in the same transaction must be rolled back."""
    user_id = 9006
    ch_id, sid = await _seed_user_channel_schedule(user_id, tg_id="-9006")
    _, post_ids = await db.add_queued_posts_bulk(
        sid, [{"media_type": "photo", "file_id": "k"}],
    )
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {"file_unique_id": "uk", "dhash": "1", "file_id": "k",
             "media_type": "photo", "queued_post_id": post_ids[0]},
        ],
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(db, "_delete_queued_post_in_tx", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await posting.cancel(post_id=post_ids[0], user_id=user_id)

    fp = await db.find_fingerprint_by_file_unique_id(ch_id, "uk")
    assert fp is not None  # rolled back
    posts = await db.get_queued_posts(sid, limit=10)
    assert [p["file_id"] for p in posts] == ["k"]
