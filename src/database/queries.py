"""Database query functions.

These are intentionally small, composable helpers used by handlers and the scheduler.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .connection import get_db, transaction
from .time import to_sqlite_timestamp


def _row_to_dict(row) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    if row is None:
        return None
    return dict(row)


# --- Users -----------------------------------------------------------------


async def upsert_user(
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    is_admin: bool = False,
) -> dict[str, Any]:
    """Insert user if missing; otherwise update metadata and last_active_at."""
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO users (id, username, first_name, last_name, is_admin)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_active_at = CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name, last_name, int(is_admin)),
        )

        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        user = _row_to_dict(row)
        assert user is not None
        return user


async def get_all_users() -> list[dict[str, Any]]:
    """Get all users (for admin broadcast)."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_users(*, since: datetime) -> list[dict[str, Any]]:
    """Get users whose last_active_at is on/after `since` (UTC)."""
    since_value = to_sqlite_timestamp(since.astimezone(timezone.utc))
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE last_active_at >= ? ORDER BY last_active_at DESC",
            (since_value,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_user_count(*, since: datetime) -> int:
    """Count users whose last_active_at is on/after `since` (UTC)."""
    since_value = to_sqlite_timestamp(since.astimezone(timezone.utc))
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE last_active_at >= ?",
            (since_value,),
        )
        row = await cursor.fetchone()
        return int(row[0])  # type: ignore[index]


# --- User context (selection) ------------------------------------------------


async def get_user_context(user_id: int) -> dict[str, Any]:
    """Get per-user selection context (selected channel/schedule).

    Returns keys:
    - selected_channel_id: internal channels.id (or None)
    - selected_schedule_id: internal schedules.id (or None)
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT selected_channel_id, selected_schedule_id
            FROM user_context
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"selected_channel_id": None, "selected_schedule_id": None}
        return dict(row)


async def get_user_context_details(user_id: int) -> dict[str, Any]:
    """Get per-user selection context with display details (best-effort)."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
              uc.selected_channel_id,
              uc.selected_schedule_id,
              c.channel_id AS telegram_channel_id,
              c.channel_name AS channel_name,
              s.name AS schedule_name,
              s.state AS schedule_state
            FROM user_context uc
            LEFT JOIN channels c ON uc.selected_channel_id = c.id
            LEFT JOIN schedules s ON uc.selected_schedule_id = s.id
            WHERE uc.user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {
                "selected_channel_id": None,
                "selected_schedule_id": None,
                "telegram_channel_id": None,
                "channel_name": None,
                "schedule_name": None,
                "schedule_state": None,
            }
        return dict(row)


async def set_user_context(
    *,
    user_id: int,
    selected_channel_id: int | None,
    selected_schedule_id: int | None,
) -> None:
    """Upsert per-user selection context."""
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO user_context (user_id, selected_channel_id, selected_schedule_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              selected_channel_id = excluded.selected_channel_id,
              selected_schedule_id = excluded.selected_schedule_id,
              updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, selected_channel_id, selected_schedule_id),
        )


async def clear_user_context(user_id: int) -> None:
    """Clear current channel/schedule selection for a user."""
    await set_user_context(user_id=user_id, selected_channel_id=None, selected_schedule_id=None)


# --- Channels ---------------------------------------------------------------


async def create_channel(
    *,
    user_id: int,
    telegram_channel_id: str,
    channel_name: str,
) -> dict[str, Any]:
    """Create a verified channel for a user."""
    async with transaction() as db:
        cursor = await db.execute(
            """
            INSERT INTO channels (user_id, channel_id, channel_name)
            VALUES (?, ?, ?)
            RETURNING *
            """,
            (user_id, telegram_channel_id, channel_name),
        )
        row = await cursor.fetchone()
        channel = _row_to_dict(row)
        assert channel is not None
        return channel


async def get_user_channels(user_id: int) -> list[dict[str, Any]]:
    """Get active channels owned by user."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM channels
            WHERE user_id = ? AND is_active = TRUE
            ORDER BY channel_name
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_channel_by_telegram_id(telegram_channel_id: str) -> dict[str, Any] | None:
    """Get channel by Telegram channel ID/username stored in channels.channel_id."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM channels WHERE channel_id = ?",
            (telegram_channel_id,),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row)


async def get_channel_by_id(channel_db_id: int) -> dict[str, Any] | None:
    """Get channel by internal DB id (channels.id)."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM channels WHERE id = ?", (channel_db_id,))
        row = await cursor.fetchone()
        return _row_to_dict(row)


async def delete_channel(channel_db_id: int) -> None:
    """Delete channel (cascades to schedules and queued posts)."""
    async with transaction() as db:
        await db.execute("DELETE FROM channels WHERE id = ?", (channel_db_id,))


async def update_channel_name(channel_db_id: int, *, channel_name: str) -> None:
    """Update stored channel name/title."""
    async with transaction() as db:
        await db.execute(
            "UPDATE channels SET channel_name = ? WHERE id = ?",
            (channel_name, channel_db_id),
        )


# --- Schedules --------------------------------------------------------------


async def create_schedule(
    *,
    channel_db_id: int,
    name: str,
    pattern: dict[str, Any],
    timezone_name: str = "UTC",
    state: str = "paused",
) -> dict[str, Any]:
    """Create a schedule for a channel (defaults to paused)."""
    async with transaction() as db:
        cursor = await db.execute(
            """
            INSERT INTO schedules (channel_id, name, pattern, timezone, state)
            VALUES (?, ?, ?, ?, ?)
            RETURNING *
            """,
            (channel_db_id, name, json.dumps(pattern), timezone_name, state),
        )
        row = await cursor.fetchone()
        schedule = _row_to_dict(row)
        assert schedule is not None
        schedule["pattern"] = json.loads(schedule["pattern"])
        return schedule


async def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    """Get schedule by ID with parsed JSON pattern."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        row = await cursor.fetchone()
        schedule = _row_to_dict(row)
        if schedule is None:
            return None
        schedule["pattern"] = json.loads(schedule["pattern"])
        return schedule


async def get_schedule_with_channel(schedule_id: int) -> dict[str, Any] | None:
    """Get schedule with owning channel details.

    Returns schedule fields plus:
    - telegram_channel_id
    - channel_name
    - owner_user_id
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT s.*,
                   c.channel_id AS telegram_channel_id,
                   c.channel_name AS channel_name,
                   c.user_id AS owner_user_id
            FROM schedules s
            JOIN channels c ON s.channel_id = c.id
            WHERE s.id = ?
            """,
            (schedule_id,),
        )
        row = await cursor.fetchone()
        schedule = _row_to_dict(row)
        if schedule is None:
            return None
        schedule["pattern"] = json.loads(schedule["pattern"])
        return schedule


async def get_schedule_for_user(user_id: int, schedule_id: int) -> dict[str, Any] | None:
    """Get schedule only if it is owned by user_id."""
    schedule = await get_schedule_with_channel(schedule_id)
    if schedule is None:
        return None
    if int(schedule["owner_user_id"]) != user_id:
        return None
    return schedule


async def get_channel_schedules(channel_db_id: int) -> list[dict[str, Any]]:
    """Get schedules for a channel (JSON pattern parsed)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM schedules WHERE channel_id = ? ORDER BY name",
            (channel_db_id,),
        )
        rows = await cursor.fetchall()
        schedules: list[dict[str, Any]] = []
        for r in rows:
            s = dict(r)
            s["pattern"] = json.loads(s["pattern"])
            schedules.append(s)
        return schedules


async def get_active_schedules() -> list[dict[str, Any]]:
    """Get active schedules with joined channel Telegram id and owner user_id."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT s.*,
                   c.channel_id AS telegram_channel_id,
                   c.channel_name AS channel_name,
                   c.user_id AS owner_user_id
            FROM schedules s
            JOIN channels c ON s.channel_id = c.id
            WHERE s.state = 'active' AND c.is_active = TRUE
            """,
        )
        rows = await cursor.fetchall()
        schedules: list[dict[str, Any]] = []
        for r in rows:
            s = dict(r)
            s["pattern"] = json.loads(s["pattern"])
            schedules.append(s)
        return schedules


async def update_schedule_state(schedule_id: int, state: str) -> None:
    """Update schedule state."""
    async with transaction() as db:
        await db.execute(
            "UPDATE schedules SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (state, schedule_id),
        )


async def update_schedule_pattern(schedule_id: int, pattern: dict[str, Any]) -> None:
    """Update schedule pattern JSON."""
    async with transaction() as db:
        await db.execute(
            "UPDATE schedules SET pattern = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(pattern), schedule_id),
        )


async def update_schedule_name(schedule_id: int, *, name: str) -> None:
    """Update schedule name."""
    async with transaction() as db:
        await db.execute(
            "UPDATE schedules SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, schedule_id),
        )


async def update_schedule_last_run(schedule_id: int) -> None:
    """Update last_run_at timestamp."""
    async with transaction() as db:
        await db.execute(
            "UPDATE schedules SET last_run_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (schedule_id,),
        )


async def delete_schedule(schedule_id: int) -> None:
    """Delete schedule (cascades to queued_posts)."""
    async with transaction() as db:
        await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# --- Queue ------------------------------------------------------------------


async def add_queued_post(
    *,
    schedule_id: int,
    media_type: str,
    file_id: str | None = None,
    file_path: str | None = None,
    caption: str | None = None,
    caption_parse_mode: str | None = None,
    caption_entities: str | None = None,
    media_group_data: str | None = None,
) -> None:
    """Add a post to the end of a schedule's FIFO queue."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM queued_posts WHERE schedule_id = ?",
            (schedule_id,),
        )
        next_position_row = await cursor.fetchone()
        next_position = int(next_position_row[0])  # type: ignore[index]

        await db.execute(
            """
            INSERT INTO queued_posts
                (
                    schedule_id,
                    file_id,
                    file_path,
                    media_type,
                    caption,
                    caption_parse_mode,
                    caption_entities,
                    media_group_data,
                    position
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                file_id,
                file_path,
                media_type,
                caption,
                caption_parse_mode,
                caption_entities,
                media_group_data,
                next_position,
            ),
        )


async def add_queued_posts_bulk(schedule_id: int, posts: list[dict[str, Any]]) -> int:
    """Add multiple queued posts in one transaction.

    Args:
        schedule_id: Schedule to append to.
        posts: List of post dicts with keys:
            - media_type (required)
            - file_id (optional)
            - file_path (optional)
            - caption (optional)
            - caption_parse_mode (optional): NULL (plain), 'markdownv2', or 'html'
            - caption_entities (optional): JSON list of Telegram MessageEntity dicts
            - media_group_data (optional)

    Returns:
        Number of inserted posts.
    """
    if not posts:
        return 0

    async with transaction() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM queued_posts WHERE schedule_id = ?",
            (schedule_id,),
        )
        start_position = int((await cursor.fetchone())[0])  # type: ignore[index]

        rows: list[tuple[Any, ...]] = []
        for i, post in enumerate(posts):
            rows.append(
                (
                    schedule_id,
                    post.get("file_id"),
                    post.get("file_path"),
                    post["media_type"],
                    post.get("caption"),
                    post.get("caption_parse_mode"),
                    post.get("caption_entities"),
                    post.get("media_group_data"),
                    start_position + i,
                )
            )

        await db.executemany(
            """
            INSERT INTO queued_posts
                (
                    schedule_id,
                    file_id,
                    file_path,
                    media_type,
                    caption,
                    caption_parse_mode,
                    caption_entities,
                    media_group_data,
                    position
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    return len(posts)


async def get_next_queued_post(schedule_id: int) -> dict[str, Any] | None:
    """Get next post from queue (lowest position)."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM queued_posts
            WHERE schedule_id = ?
            ORDER BY position ASC
            LIMIT 1
            """,
            (schedule_id,),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row)


async def get_queued_posts(schedule_id: int, *, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    """Get posts from queue in FIFO order."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM queued_posts
            WHERE schedule_id = ?
            ORDER BY position ASC
            LIMIT ? OFFSET ?
            """,
            (schedule_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_queued_posts_unscheduled(schedule_id: int, *, limit: int) -> list[dict[str, Any]]:
    """Get queued posts that do not have scheduled_for set, in FIFO order."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM queued_posts
            WHERE schedule_id = ? AND scheduled_for IS NULL
            ORDER BY position ASC
            LIMIT ?
            """,
            (schedule_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_queue_count(schedule_id: int) -> int:
    """Count posts in a schedule queue."""
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM queued_posts WHERE schedule_id = ?", (schedule_id,))
        row = await cursor.fetchone()
        return int(row[0])  # type: ignore[index]


async def delete_queued_post(post_id: int) -> None:
    """Delete post from queue and compact positions for FIFO ordering."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT schedule_id, position FROM queued_posts WHERE id = ?",
            (post_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        schedule_id = int(row[0])  # type: ignore[index]
        deleted_position = int(row[1])  # type: ignore[index]

        await db.execute("DELETE FROM queued_posts WHERE id = ?", (post_id,))
        await db.execute(
            """
            UPDATE queued_posts
            SET position = position - 1
            WHERE schedule_id = ? AND position > ?
            """,
            (schedule_id, deleted_position),
        )


async def get_queued_post_with_owner(post_id: int) -> dict[str, Any] | None:
    """Get a queued post along with owner info for permission checks."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT qp.*,
                   c.user_id AS owner_user_id
            FROM queued_posts qp
            JOIN schedules s ON qp.schedule_id = s.id
            JOIN channels c ON s.channel_id = c.id
            WHERE qp.id = ?
            """,
            (post_id,),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row)


async def update_post_scheduled_for(post_id: int, *, scheduled_for: datetime | None) -> None:
    """Set (or clear) scheduled_for for a queued post."""
    scheduled_for_value = None if scheduled_for is None else to_sqlite_timestamp(scheduled_for)
    async with transaction() as db:
        await db.execute(
            "UPDATE queued_posts SET scheduled_for = ? WHERE id = ?",
            (scheduled_for_value, post_id),
        )


async def bulk_update_posts_scheduled_for(post_updates: list[tuple[int, datetime]]) -> None:
    """Set scheduled_for for multiple posts in one transaction.

    Args:
        post_updates: List of (post_id, scheduled_for) pairs.
    """
    if not post_updates:
        return

    params = [(to_sqlite_timestamp(scheduled_for), post_id) for (post_id, scheduled_for) in post_updates]
    async with transaction() as db:
        await db.executemany(
            "UPDATE queued_posts SET scheduled_for = ? WHERE id = ?",
            params,
        )


async def update_post_retry(post_id: int, *, retry_count: int, scheduled_for: datetime) -> None:
    """Update retry count and next attempt time."""
    async with transaction() as db:
        await db.execute(
            "UPDATE queued_posts SET retry_count = ?, scheduled_for = ? WHERE id = ?",
            (retry_count, to_sqlite_timestamp(scheduled_for), post_id),
        )


async def get_earliest_scheduled_for() -> Any:
    """Get the earliest scheduled_for value across all queued posts (or None)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT MIN(scheduled_for) FROM queued_posts WHERE scheduled_for IS NOT NULL"
        )
        row = await cursor.fetchone()
        return row[0] if row else None  # type: ignore[index]


# --- Verification codes ------------------------------------------------------


VERIFICATION_CODE_LIFETIME = timedelta(minutes=10)


async def create_verification_code(*, user_id: int, telegram_channel_id: str) -> str:
    """Generate a new verification code for (user_id, channel_id)."""
    code = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + VERIFICATION_CODE_LIFETIME

    async with transaction() as db:
        # Invalidate previous codes for this user+channel
        await db.execute(
            "UPDATE verification_codes SET used = TRUE WHERE user_id = ? AND channel_id = ?",
            (user_id, telegram_channel_id),
        )

        await db.execute(
            """
            INSERT INTO verification_codes (user_id, channel_id, code, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, telegram_channel_id, code, to_sqlite_timestamp(expires_at)),
        )

    return code


async def verify_code(*, code: str, telegram_channel_id: str) -> int | None:
    """Verify code and mark as used. Returns user_id if valid."""
    async with transaction() as db:
        cursor = await db.execute(
            """
            SELECT user_id
            FROM verification_codes
            WHERE code = ?
              AND channel_id = ?
              AND used = FALSE
              AND expires_at > CURRENT_TIMESTAMP
            """,
            (code, telegram_channel_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        user_id = int(row[0])  # type: ignore[index]
        await db.execute("UPDATE verification_codes SET used = TRUE WHERE code = ?", (code,))
        return user_id


async def cleanup_expired_codes() -> None:
    """Delete expired verification codes."""
    async with transaction() as db:
        await db.execute("DELETE FROM verification_codes WHERE expires_at < CURRENT_TIMESTAMP")


# --- Admin / stats ----------------------------------------------------------


async def get_system_stats() -> dict[str, int]:
    """Get basic system statistics for admin commands."""
    async with get_db() as db:
        stats: dict[str, int] = {}

        cursor = await db.execute("SELECT COUNT(*) FROM users")
        stats["total_users"] = int((await cursor.fetchone())[0])  # type: ignore[index]

        cursor = await db.execute("SELECT COUNT(*) FROM channels WHERE is_active = TRUE")
        stats["total_channels"] = int((await cursor.fetchone())[0])  # type: ignore[index]

        cursor = await db.execute("SELECT COUNT(*) FROM schedules WHERE state = 'active'")
        stats["active_schedules"] = int((await cursor.fetchone())[0])  # type: ignore[index]

        cursor = await db.execute("SELECT COUNT(*) FROM queued_posts")
        stats["queued_posts"] = int((await cursor.fetchone())[0])  # type: ignore[index]

        cursor = await db.execute("SELECT COUNT(*) FROM queued_posts WHERE retry_count > 0")
        stats["failed_posts"] = int((await cursor.fetchone())[0])  # type: ignore[index]

        return stats


async def get_schedule_state_counts() -> dict[str, int]:
    """Count schedules by state."""
    async with get_db() as db:
        cursor = await db.execute("SELECT state, COUNT(*) FROM schedules GROUP BY state")
        rows = await cursor.fetchall()
        out: dict[str, int] = {"active": 0, "paused": 0, "empty_paused": 0}
        for r in rows:
            state = str(r[0])  # type: ignore[index]
            count = int(r[1])  # type: ignore[index]
            out[state] = count
        return out


async def increment_delivery_stats_daily(
    *,
    day: date,
    posts_sent_delta: int = 0,
    send_failures_delta: int = 0,
) -> None:
    """Increment aggregated daily delivery counters (UTC day)."""
    if posts_sent_delta == 0 and send_failures_delta == 0:
        return

    day_str = day.isoformat()
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO delivery_stats_daily (day, posts_sent, send_failures)
            VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                posts_sent = posts_sent + excluded.posts_sent,
                send_failures = send_failures + excluded.send_failures,
                updated_at = CURRENT_TIMESTAMP
            """,
            (day_str, int(posts_sent_delta), int(send_failures_delta)),
        )


async def get_delivery_stats_sum_since(*, since_day: date) -> dict[str, int]:
    """Sum delivery stats for days >= since_day (inclusive)."""
    since_str = since_day.isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
              COALESCE(SUM(posts_sent), 0),
              COALESCE(SUM(send_failures), 0)
            FROM delivery_stats_daily
            WHERE day >= ?
            """,
            (since_str,),
        )
        row = await cursor.fetchone()
        return {
            "posts_sent": int(row[0]),  # type: ignore[index]
            "send_failures": int(row[1]),  # type: ignore[index]
        }

