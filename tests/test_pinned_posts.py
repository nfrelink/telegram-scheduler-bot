"""Tests for Phase 7: one-time date-specific (pinned) posts.

Covers:
- DB helpers: set_post_pinned_at, clear_post_pinned_at, get_earliest_pinned_at
- get_next_queued_post two-tier priority (pinned before FIFO)
- get_queued_posts_unscheduled excludes pinned posts
- _parse_date_input and _parse_time_input utility functions
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from database import queries as db
from database.time import parse_timestamp
from handlers.queue_management import _parse_date_input, _parse_time_input

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_schedule(user_id: int, channel_suffix: str) -> int:
    channel = await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-99{channel_suffix}",
        channel_name=f"Chan-{channel_suffix}",
    )
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name=f"Sched-{channel_suffix}",
        pattern={"type": "interval", "hours": 1},
        timezone_name="UTC",
        state="active",
    )
    return int(schedule["id"])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_clear_post_pinned_at(initialized_db) -> None:
    user_id = 7001
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "001")

    await db.add_queued_posts_bulk(schedule_id, [{"media_type": "photo", "file_id": "x1"}])
    posts = await db.get_queued_posts(schedule_id, limit=1)
    post_id = int(posts[0]["id"])

    # Initially pinned_at is NULL.
    assert posts[0].get("pinned_at") is None

    target = datetime(2026, 12, 25, 20, 0, 0, tzinfo=UTC)
    await db.set_post_pinned_at(post_id, target, user_id=user_id)

    posts = await db.get_queued_posts(schedule_id, limit=1)
    stored = parse_timestamp(posts[0].get("pinned_at"))
    assert stored is not None
    assert stored == target.replace(microsecond=0)

    await db.clear_post_pinned_at(post_id, user_id=user_id)
    posts = await db.get_queued_posts(schedule_id, limit=1)
    assert posts[0].get("pinned_at") is None


@pytest.mark.asyncio
async def test_get_earliest_pinned_at_returns_none_when_no_pins(initialized_db) -> None:
    user_id = 7002
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "002")
    await db.add_queued_posts_bulk(schedule_id, [{"media_type": "photo", "file_id": "y1"}])

    result = await db.get_earliest_pinned_at()
    assert result is None


@pytest.mark.asyncio
async def test_get_earliest_pinned_at_returns_min_value(initialized_db) -> None:
    user_id = 7003
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "003")

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "a"},
            {"media_type": "photo", "file_id": "b"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=2)
    id_a, id_b = int(posts[0]["id"]), int(posts[1]["id"])

    later = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
    earlier = datetime(2026, 12, 25, 20, 0, tzinfo=UTC)
    await db.set_post_pinned_at(id_a, later, user_id=user_id)
    await db.set_post_pinned_at(id_b, earlier, user_id=user_id)

    raw = await db.get_earliest_pinned_at()
    result = parse_timestamp(raw)
    assert result is not None
    assert result == earlier.replace(microsecond=0)


# ---------------------------------------------------------------------------
# get_next_queued_post two-tier priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_next_queued_post_returns_due_pinned_before_fifo(
    initialized_db,
) -> None:
    """A pinned post whose pinned_at <= now is returned ahead of FIFO posts."""
    user_id = 7004
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "004")

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "fifo_first"},
            {"media_type": "photo", "file_id": "fifo_second"},
            {"media_type": "photo", "file_id": "will_be_pinned"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=10)
    pinned_id = next(p for p in posts if p["file_id"] == "will_be_pinned")["id"]

    past = datetime.now(UTC) - timedelta(hours=1)
    await db.set_post_pinned_at(int(pinned_id), past, user_id=user_id)

    now = datetime.now(UTC)
    next_post = await db.get_next_queued_post(schedule_id, now=now)
    assert next_post is not None
    assert next_post["file_id"] == "will_be_pinned"


@pytest.mark.asyncio
async def test_get_next_queued_post_ignores_future_pinned_and_returns_fifo(
    initialized_db,
) -> None:
    """A pinned post whose pinned_at is in the future is excluded; the FIFO head is returned."""
    user_id = 7005
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "005")

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "fifo_head"},
            {"media_type": "photo", "file_id": "future_pin"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=10)
    future_pin_id = next(p for p in posts if p["file_id"] == "future_pin")["id"]

    future = datetime.now(UTC) + timedelta(days=30)
    await db.set_post_pinned_at(int(future_pin_id), future, user_id=user_id)

    now = datetime.now(UTC)
    next_post = await db.get_next_queued_post(schedule_id, now=now)
    assert next_post is not None
    assert next_post["file_id"] == "fifo_head"


@pytest.mark.asyncio
async def test_get_next_queued_post_earliest_pinned_wins_when_multiple_due(
    initialized_db,
) -> None:
    """When multiple pinned posts are due, the one with the earliest pinned_at is returned."""
    user_id = 7006
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "006")

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "pin_later"},
            {"media_type": "photo", "file_id": "pin_earlier"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=10)
    id_later = next(p for p in posts if p["file_id"] == "pin_later")["id"]
    id_earlier = next(p for p in posts if p["file_id"] == "pin_earlier")["id"]

    now = datetime.now(UTC)
    await db.set_post_pinned_at(int(id_later), now - timedelta(hours=1), user_id=user_id)
    await db.set_post_pinned_at(int(id_earlier), now - timedelta(hours=2), user_id=user_id)

    next_post = await db.get_next_queued_post(schedule_id, now=now)
    assert next_post is not None
    assert next_post["file_id"] == "pin_earlier"


# ---------------------------------------------------------------------------
# get_queued_posts_unscheduled excludes pinned posts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_queued_posts_unscheduled_excludes_pinned(initialized_db) -> None:
    """Pinned posts must not be returned by get_queued_posts_unscheduled so the
    catch-up scheduler cannot overwrite their pinned_at with a scheduled_for value."""
    user_id = 7007
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    schedule_id = await _make_schedule(user_id, "007")

    await db.add_queued_posts_bulk(
        schedule_id,
        [
            {"media_type": "photo", "file_id": "normal"},
            {"media_type": "photo", "file_id": "pinned"},
        ],
    )
    posts = await db.get_queued_posts(schedule_id, limit=10)
    pinned_id = next(p for p in posts if p["file_id"] == "pinned")["id"]

    future = datetime.now(UTC) + timedelta(days=7)
    await db.set_post_pinned_at(int(pinned_id), future, user_id=user_id)

    unscheduled = await db.get_queued_posts_unscheduled(schedule_id, limit=10)
    file_ids = [p["file_id"] for p in unscheduled]
    assert "normal" in file_ids
    assert "pinned" not in file_ids


# ---------------------------------------------------------------------------
# _parse_date_input
# ---------------------------------------------------------------------------

TODAY = date(2026, 3, 16)  # fixed reference date for deterministic tests


@pytest.mark.parametrize(
    "text,expected",
    [
        # DD/MM/YYYY
        ("25/12/2026", date(2026, 12, 25)),
        ("01/01/2027", date(2027, 1, 1)),
        # DD/MM — infer nearest future year
        ("25/12", date(2026, 12, 25)),
        ("10/03", date(2027, 3, 10)),  # already passed in 2026 (today is 16 Mar)
        # DD Mon YYYY
        ("25 Dec 2026", date(2026, 12, 25)),
        ("1 Jan 2027", date(2027, 1, 1)),
        # DD Mon — infer nearest future year
        ("25 Dec", date(2026, 12, 25)),
        ("5 Mar", date(2027, 3, 5)),  # already passed in 2026
        # DD Month YYYY (full name)
        ("25 December 2026", date(2026, 12, 25)),
    ],
)
def test_parse_date_input_valid(text: str, expected: date) -> None:
    result = _parse_date_input(text, now=TODAY)
    assert result == expected, f"For {text!r}: expected {expected}, got {result}"


@pytest.mark.parametrize(
    "text",
    [
        "not a date",
        "32/01/2026",  # invalid day
        "00/00",
        "2026-12-25",  # ISO format not supported
        "",
    ],
)
def test_parse_date_input_invalid(text: str) -> None:
    assert _parse_date_input(text, now=TODAY) is None


# ---------------------------------------------------------------------------
# _parse_time_input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20:00", (20, 0)),
        ("09:30", (9, 30)),
        ("0:00", (0, 0)),
        ("23:59", (23, 59)),
        ("2000", (20, 0)),  # HHMM compact form
        ("0930", (9, 30)),
    ],
)
def test_parse_time_input_valid(text: str, expected: tuple[int, int]) -> None:
    assert _parse_time_input(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "25:00",  # hour out of range
        "20:60",  # minute out of range
        "noon",
        "",
        "12",
    ],
)
def test_parse_time_input_invalid(text: str) -> None:
    assert _parse_time_input(text) is None
