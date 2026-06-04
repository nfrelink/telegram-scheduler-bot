"""Database query functions.

These are intentionally small, composable helpers used by handlers and the scheduler.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .connection import get_db, transaction
from .time import parse_timestamp, to_sqlite_timestamp


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
        if user is None:
            raise RuntimeError(
                f"upsert_user: SELECT after UPSERT returned no row for user_id={user_id}"
            )
        return user


async def get_user_timezone(user_id: int) -> str | None:
    """Get the user's preferred timezone (IANA name), or None if unset."""
    async with get_db() as db:
        cursor = await db.execute("SELECT timezone FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        # Row is a sqlite Row; index 0 corresponds to 'timezone'.
        raw = row[0]  # type: ignore[index]
        if raw is None:
            return None
        tz = str(raw).strip()
        return tz or None


async def set_user_timezone(user_id: int, timezone_name: str | None) -> None:
    """Set the user's preferred timezone (IANA name). Use None to clear."""
    value = timezone_name.strip() if isinstance(timezone_name, str) else None
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET timezone = ? WHERE id = ?",
            (value, user_id),
        )


async def get_all_users() -> list[dict[str, Any]]:
    """Get all users (for admin broadcast)."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_users(*, since: datetime) -> list[dict[str, Any]]:
    """Get users whose last_active_at is on/after `since` (UTC)."""
    since_value = to_sqlite_timestamp(since.astimezone(UTC))
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE last_active_at >= ? ORDER BY last_active_at DESC",
            (since_value,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_user_count(*, since: datetime) -> int:
    """Count users whose last_active_at is on/after `since` (UTC)."""
    since_value = to_sqlite_timestamp(since.astimezone(UTC))
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


# --- Forwarding allowlist ----------------------------------------------------


async def get_forward_origin_allowlist(user_id: int) -> list[int]:
    """Get origin chat IDs (channels) that should be forwarded for this user."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT origin_chat_id
            FROM forward_origin_allowlist
            WHERE user_id = ?
            ORDER BY origin_chat_id ASC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]  # type: ignore[index]


async def get_forward_origin_allowlist_with_names(
    user_id: int,
) -> list[tuple[int, str | None]]:
    """Get origin chat IDs with their stored channel names for display."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT origin_chat_id, origin_channel_name
            FROM forward_origin_allowlist
            WHERE user_id = ?
            ORDER BY origin_chat_id ASC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [(int(r[0]), str(r[1]) if r[1] else None) for r in rows]  # type: ignore[index]


async def add_forward_origin_allowlist(
    *, user_id: int, origin_chat_id: int, origin_channel_name: str | None = None
) -> None:
    """Add an origin chat ID to a user's forwarding allowlist."""
    async with transaction() as db:
        await db.execute(
            """
            INSERT INTO forward_origin_allowlist (user_id, origin_chat_id, origin_channel_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, origin_chat_id) DO UPDATE SET
                origin_channel_name = COALESCE(excluded.origin_channel_name, origin_channel_name)
            """,
            (user_id, origin_chat_id, origin_channel_name),
        )


async def remove_forward_origin_allowlist(*, user_id: int, origin_chat_id: int) -> None:
    """Remove an origin chat ID from a user's forwarding allowlist."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM forward_origin_allowlist WHERE user_id = ? AND origin_chat_id = ?",
            (user_id, origin_chat_id),
        )


async def clear_forward_origin_allowlist(user_id: int) -> None:
    """Clear forwarding allowlist for a user."""
    async with transaction() as db:
        await db.execute("DELETE FROM forward_origin_allowlist WHERE user_id = ?", (user_id,))


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


async def delete_channel(channel_db_id: int, *, user_id: int) -> None:
    """Delete channel (cascades to schedules and queued posts)."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM channels WHERE id = ? AND user_id = ?",
            (channel_db_id, user_id),
        )


async def update_channel_name(channel_db_id: int, *, channel_name: str, user_id: int) -> None:
    """Update stored channel name/title."""
    async with transaction() as db:
        await db.execute(
            "UPDATE channels SET channel_name = ? WHERE id = ? AND user_id = ?",
            (channel_name, channel_db_id, user_id),
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


async def _update_schedule_state_in_tx(db, schedule_id: int, state: str, *, user_id: int) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for update_schedule_state. Caller owns the tx.

    Also clears `next_planned_run_at` when the new state is not 'active'. This
    keeps the invariant "active schedules carry a non-NULL NPR; non-active
    schedules carry NULL" enforced atomically. Resuming (transition back to
    active) goes through `resume_schedule` followed by `recompute_next_run`,
    which both set NPR explicitly.
    """
    if state == "active":
        await db.execute(
            """
            UPDATE schedules SET state = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (state, schedule_id, user_id),
        )
    else:
        await db.execute(
            """
            UPDATE schedules
            SET state = ?, updated_at = CURRENT_TIMESTAMP, next_planned_run_at = NULL
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (state, schedule_id, user_id),
        )


async def update_schedule_state(schedule_id: int, state: str, *, user_id: int) -> None:
    """Update schedule state."""
    async with transaction() as db:
        await _update_schedule_state_in_tx(db, schedule_id, state, user_id=user_id)


async def resume_schedule(schedule_id: int, *, user_id: int) -> None:
    """Set a schedule active. Stamps `last_run_at` to now for audit/history.

    The "fires at the next scheduled slot, not immediately" guarantee is
    provided by `services.scheduling.resume()`, which calls
    `recompute_next_run` after this query returns; this query itself does
    not read or set `next_planned_run_at`.
    """
    async with transaction() as db:
        await db.execute(
            """
            UPDATE schedules
            SET state = 'active',
                last_run_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (schedule_id, user_id),
        )


async def update_schedule_pattern(
    schedule_id: int, pattern: dict[str, Any], *, user_id: int
) -> None:
    """Update schedule pattern JSON."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE schedules SET pattern = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (json.dumps(pattern), schedule_id, user_id),
        )


async def update_schedule_name(schedule_id: int, *, name: str, user_id: int) -> None:
    """Update schedule name."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE schedules SET name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (name, schedule_id, user_id),
        )


async def update_schedule_timezone(schedule_id: int, *, timezone_name: str, user_id: int) -> None:
    """Update schedule timezone (IANA name)."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE schedules SET timezone = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)
            """,
            (timezone_name, schedule_id, user_id),
        )


async def _update_schedule_last_run_in_tx(db, schedule_id: int) -> None:  # type: ignore[no-untyped-def]
    """Stamp `last_run_at = CURRENT_TIMESTAMP` for `schedule_id`.

    Called from `services.posting.complete_send` inside a larger
    transaction; no public standalone wrapper exists because no
    out-of-tx caller currently needs one.
    """
    await db.execute(
        "UPDATE schedules SET last_run_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (schedule_id,),
    )


async def _update_schedule_next_planned_run_in_tx(
    db, schedule_id: int, next_at: datetime | None
) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for update_schedule_next_planned_run. Caller owns the tx.

    `next_at` is stored as a UTC TEXT timestamp; pass None to clear (e.g. when a
    schedule transitions to paused/empty_paused).
    """
    await db.execute(
        "UPDATE schedules SET next_planned_run_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (to_sqlite_timestamp(next_at) if next_at is not None else None, schedule_id),
    )


async def update_schedule_next_planned_run(schedule_id: int, next_at: datetime | None) -> None:
    """Persist the schedule's next planned fire time (UTC) or clear it.

    Single source of truth used by the scheduler tick; written by
    `recompute_next_run` and (atomically) by `complete_post_send`.
    """
    async with transaction() as db:
        await _update_schedule_next_planned_run_in_tx(db, schedule_id, next_at)


async def delete_schedule(schedule_id: int, *, user_id: int) -> None:
    """Delete schedule (cascades to queued_posts)."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM schedules WHERE id = ? AND channel_id IN (SELECT id FROM channels WHERE user_id = ?)",
            (schedule_id, user_id),
        )


# --- Queue ------------------------------------------------------------------


async def add_queued_posts_bulk(
    schedule_id: int, posts: list[dict[str, Any]]
) -> tuple[int, list[int]]:
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
            - forward_from_chat_id (optional): Telegram chat id to forward FROM
            - forward_from_message_id (optional): Telegram message id in forward_from_chat_id
            - forward_origin_chat_id (optional): Original source chat id (e.g., channel id)
            - forward_origin_message_id (optional): Original source message id (e.g., channel post id)
            - media_group_data (optional)

    Returns:
        Tuple of (number of inserted posts, list of inserted post IDs).
    """
    if not posts:
        return 0, []

    for post in posts:
        if post.get("media_type") == "media_group" and not post.get("media_group_data"):
            raise ValueError(
                f"Cannot queue media_group post without media_group_data (schedule_id={schedule_id})"
            )

    async with transaction() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM queued_posts WHERE schedule_id = ?",
            (schedule_id,),
        )
        start_position = int((await cursor.fetchone())[0])  # type: ignore[index]

        inserted_ids: list[int] = []
        for i, post in enumerate(posts):
            cursor = await db.execute(
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
                        forward_from_chat_id,
                        forward_from_message_id,
                        forward_origin_chat_id,
                        forward_origin_message_id,
                        media_group_data,
                        position
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    schedule_id,
                    post.get("file_id"),
                    post.get("file_path"),
                    post["media_type"],
                    post.get("caption"),
                    post.get("caption_parse_mode"),
                    post.get("caption_entities"),
                    post.get("forward_from_chat_id"),
                    post.get("forward_from_message_id"),
                    post.get("forward_origin_chat_id"),
                    post.get("forward_origin_message_id"),
                    post.get("media_group_data"),
                    start_position + i,
                ),
            )
            row = await cursor.fetchone()
            inserted_ids.append(int(row[0]))  # type: ignore[index]

    return len(posts), inserted_ids


async def get_next_queued_post(schedule_id: int, *, now: datetime) -> dict[str, Any] | None:
    """Get the next post to send for a schedule.

    Two-tier priority:
    1. Pinned posts whose pinned_at <= now, ordered by pinned_at (earliest first).
    2. Normal FIFO posts (pinned_at IS NULL), ordered by position.

    Pinned posts whose time has not yet come are excluded entirely; they do not
    block FIFO delivery of posts behind them.
    """
    now_str = to_sqlite_timestamp(now)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM queued_posts
            WHERE schedule_id = ?
              AND (pinned_at IS NULL OR pinned_at <= ?)
            ORDER BY
              CASE WHEN pinned_at IS NOT NULL THEN 0 ELSE 1 END,
              pinned_at ASC,
              position ASC
            LIMIT 1
            """,
            (schedule_id, now_str),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row)


async def get_queued_posts(
    schedule_id: int, *, limit: int = 10, offset: int = 0
) -> list[dict[str, Any]]:
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


async def count_queued_posts(schedule_id: int) -> int:
    """Return the number of posts currently in `schedule_id`'s queue.

    Used by the engine's pause-detection sites to include queue depth in
    the admin DM, so an operator can tell at a glance whether a paused
    schedule has a backlog waiting to drain or is empty.
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM queued_posts WHERE schedule_id = ?",
            (schedule_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0
        return int(row["n"])


async def get_latest_active_schedule_run_at() -> datetime | None:
    """Return MAX(last_run_at) across active schedules, or None if there
    are no active schedules (or none have ever fired).

    Used by the scheduler heartbeat: if active schedules exist but the
    most recent fire is older than HEARTBEAT_MAX_HOURS, the loop is
    presumed wedged and the admin gets pinged.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT MAX(last_run_at) AS latest
            FROM schedules
            WHERE state = 'active'
            """,
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return parse_timestamp(row["latest"])


async def get_queued_posts_unscheduled(schedule_id: int, *, limit: int) -> list[dict[str, Any]]:
    """Get queued posts that do not have scheduled_for set, in FIFO order.

    Excludes pinned posts (pinned_at IS NOT NULL) since they manage their own
    send timing and must not be given a catch-up scheduled_for value.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM queued_posts
            WHERE schedule_id = ? AND scheduled_for IS NULL AND pinned_at IS NULL
            ORDER BY position ASC
            LIMIT ?
            """,
            (schedule_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_channel_queue_count(channel_db_id: int) -> int:
    """Count total queued posts across all schedules for a channel."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM queued_posts qp
            JOIN schedules s ON qp.schedule_id = s.id
            WHERE s.channel_id = ?
            """,
            (channel_db_id,),
        )
        row = await cursor.fetchone()
        return int(row[0])  # type: ignore[index]


async def get_queue_count(schedule_id: int) -> int:
    """Count posts in a schedule queue."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM queued_posts WHERE schedule_id = ?", (schedule_id,)
        )
        row = await cursor.fetchone()
        return int(row[0])  # type: ignore[index]


async def _delete_queued_post_in_tx(db, post_id: int, *, user_id: int) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for delete_queued_post. Caller owns the tx.

    Silently no-ops if the post doesn't exist or isn't owned by user_id —
    matches the public function's defence-in-depth ownership check.
    """
    cursor = await db.execute(
        """
        SELECT qp.schedule_id, qp.position
        FROM queued_posts qp
        JOIN schedules s ON qp.schedule_id = s.id
        JOIN channels c ON s.channel_id = c.id
        WHERE qp.id = ? AND c.user_id = ?
        """,
        (post_id, user_id),
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


async def delete_queued_post(post_id: int, *, user_id: int) -> None:
    """Delete post from queue and compact positions for FIFO ordering."""
    async with transaction() as db:
        await _delete_queued_post_in_tx(db, post_id, user_id=user_id)


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


async def bulk_update_posts_scheduled_for(
    post_updates: list[tuple[int, datetime]],
) -> None:
    """Set scheduled_for for multiple posts in one transaction.

    Args:
        post_updates: List of (post_id, scheduled_for) pairs.
    """
    if not post_updates:
        return

    params = [
        (to_sqlite_timestamp(scheduled_for), post_id) for (post_id, scheduled_for) in post_updates
    ]
    async with transaction() as db:
        await db.executemany(
            "UPDATE queued_posts SET scheduled_for = ? WHERE id = ?",
            params,
        )


async def _update_post_retry_in_tx(
    db, post_id: int, *, retry_count: int, scheduled_for: datetime
) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for update_post_retry. Caller owns the tx."""
    await db.execute(
        "UPDATE queued_posts SET retry_count = ?, scheduled_for = ? WHERE id = ?",
        (retry_count, to_sqlite_timestamp(scheduled_for), post_id),
    )


async def update_post_retry(post_id: int, *, retry_count: int, scheduled_for: datetime) -> None:
    """Update retry count and next attempt time."""
    async with transaction() as db:
        await _update_post_retry_in_tx(
            db, post_id, retry_count=retry_count, scheduled_for=scheduled_for
        )


async def get_earliest_scheduled_for() -> Any:
    """Get the earliest scheduled_for value across all queued posts (or None)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT MIN(scheduled_for) FROM queued_posts WHERE scheduled_for IS NOT NULL"
        )
        row = await cursor.fetchone()
        return row[0] if row else None  # type: ignore[index]


async def get_earliest_pinned_at() -> Any:
    """Get the earliest future pinned_at value across all queued posts (or None)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT MIN(pinned_at) FROM queued_posts WHERE pinned_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        return row[0] if row else None  # type: ignore[index]


async def set_post_pinned_at(post_id: int, pinned_at: datetime, *, user_id: int) -> None:
    """Pin a queued post to a specific send datetime."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE queued_posts SET pinned_at = ?
            WHERE id = ? AND schedule_id IN (
                SELECT s.id FROM schedules s
                JOIN channels c ON s.channel_id = c.id
                WHERE c.user_id = ?
            )
            """,
            (to_sqlite_timestamp(pinned_at), post_id, user_id),
        )


async def clear_post_pinned_at(post_id: int, *, user_id: int) -> None:
    """Remove the pinned_at datetime from a queued post, returning it to FIFO."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE queued_posts SET pinned_at = NULL
            WHERE id = ? AND schedule_id IN (
                SELECT s.id FROM schedules s
                JOIN channels c ON s.channel_id = c.id
                WHERE c.user_id = ?
            )
            """,
            (post_id, user_id),
        )


# --- Verification codes ------------------------------------------------------


VERIFICATION_CODE_LIFETIME = timedelta(minutes=10)


async def create_verification_code(*, user_id: int, telegram_channel_id: str) -> str:
    """Generate a new verification code for (user_id, channel_id)."""
    code = secrets.token_urlsafe(16)
    expires_at = datetime.now(UTC) + VERIFICATION_CODE_LIFETIME

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


async def _increment_delivery_stats_daily_in_tx(
    db,
    *,
    day: date,
    posts_sent_delta: int = 0,
    send_failures_delta: int = 0,
) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for increment_delivery_stats_daily. Caller owns the tx."""
    if posts_sent_delta == 0 and send_failures_delta == 0:
        return

    day_str = day.isoformat()
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


async def increment_delivery_stats_daily(
    *,
    day: date,
    posts_sent_delta: int = 0,
    send_failures_delta: int = 0,
) -> None:
    """Increment aggregated daily delivery counters (UTC day)."""
    if posts_sent_delta == 0 and send_failures_delta == 0:
        return

    async with transaction() as db:
        await _increment_delivery_stats_daily_in_tx(
            db,
            day=day,
            posts_sent_delta=posts_sent_delta,
            send_failures_delta=send_failures_delta,
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


# --- Bulk upload staging ---------------------------------------------------


async def create_bulk_session(
    user_id: int,
    schedule_id: int,
    caption_mode: str,
    single_caption: str | None = None,
    single_caption_entities: str | None = None,
) -> None:
    """Create or replace a bulk upload session for a user."""
    async with transaction() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO bulk_upload_session
                (user_id, schedule_id, caption_mode, single_caption, single_caption_entities)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                schedule_id,
                caption_mode,
                single_caption,
                single_caption_entities,
            ),
        )


async def get_bulk_session(user_id: int) -> dict[str, Any] | None:
    """Return the active bulk upload session for a user, or None."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM bulk_upload_session WHERE user_id = ?", (user_id,))
        return _row_to_dict(await cursor.fetchone())


async def update_bulk_session_caption(
    user_id: int,
    caption_mode: str,
    single_caption: str | None = None,
    single_caption_entities: str | None = None,
) -> None:
    """Update caption settings on an existing bulk session."""
    async with transaction() as db:
        await db.execute(
            """
            UPDATE bulk_upload_session
            SET caption_mode = ?, single_caption = ?, single_caption_entities = ?
            WHERE user_id = ?
            """,
            (caption_mode, single_caption, single_caption_entities, user_id),
        )


async def add_staging_item(
    user_id: int,
    *,
    media_type: str,
    file_id: str | None = None,
    caption: str | None = None,
    caption_entities: str | None = None,
    media_group_id: str | None = None,
    forward_from_chat_id: int | None = None,
    forward_from_message_id: int | None = None,
    forward_origin_chat_id: int | None = None,
    forward_origin_message_id: int | None = None,
    raw_origin_chat_id: int | None = None,
    raw_origin_message_id: int | None = None,
    raw_origin_is_forwarded: bool = False,
) -> None:
    """Persist a collected media item to the staging table."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM bulk_staging WHERE user_id = ?",
            (user_id,),
        )
        pos = int((await cursor.fetchone())[0])  # type: ignore[index]
        await db.execute(
            """
            INSERT INTO bulk_staging
                (user_id, media_type, file_id, caption, caption_entities,
                 media_group_id, forward_from_chat_id, forward_from_message_id,
                 forward_origin_chat_id, forward_origin_message_id,
                 raw_origin_chat_id, raw_origin_message_id,
                 raw_origin_is_forwarded, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                media_type,
                file_id,
                caption,
                caption_entities,
                media_group_id,
                forward_from_chat_id,
                forward_from_message_id,
                forward_origin_chat_id,
                forward_origin_message_id,
                raw_origin_chat_id,
                raw_origin_message_id,
                raw_origin_is_forwarded,
                pos,
            ),
        )


async def get_staging_items(user_id: int) -> list[dict[str, Any]]:
    """Return all staging items for a user, ordered by position."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM bulk_staging WHERE user_id = ? ORDER BY position",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_staging_count(user_id: int) -> int:
    """Return the number of staged items for a user."""
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bulk_staging WHERE user_id = ?", (user_id,))
        return int((await cursor.fetchone())[0])  # type: ignore[index]


async def clear_staging(user_id: int) -> None:
    """Delete all staging items and the session for a user."""
    async with transaction() as db:
        await db.execute("DELETE FROM bulk_staging WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM bulk_upload_session WHERE user_id = ?", (user_id,))


# --- Media fingerprints (duplicate detection) -----------------------------


async def get_channel_duplicate_detection(channel_db_id: int) -> bool:
    """Return True if duplicate detection is enabled for a channel."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT duplicate_detection_enabled FROM channels WHERE id = ?",
            (channel_db_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        return bool(row[0])  # type: ignore[index]


async def set_channel_duplicate_detection(channel_db_id: int, *, enabled: bool) -> None:
    """Enable or disable duplicate detection for a channel."""
    async with transaction() as db:
        await db.execute(
            "UPDATE channels SET duplicate_detection_enabled = ? WHERE id = ?",
            (int(enabled), channel_db_id),
        )


async def get_user_duplicate_alerts(user_id: int) -> bool:
    """Return True if duplicate alerts are enabled for a user (default True)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT duplicate_alerts_enabled FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return True
        val = row[0]  # type: ignore[index]
        return val is None or bool(val)


async def set_user_duplicate_alerts(user_id: int, *, enabled: bool) -> None:
    """Enable or disable duplicate alerts for a user."""
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET duplicate_alerts_enabled = ? WHERE id = ?",
            (int(enabled), user_id),
        )


async def find_fingerprint_by_file_unique_id(
    channel_db_id: int, file_unique_id: str
) -> dict[str, Any] | None:
    """Find a fingerprint by exact file_unique_id match for a channel."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT * FROM media_fingerprints
            WHERE channel_id = ? AND file_unique_id = ?
            LIMIT 1
            """,
            (channel_db_id, file_unique_id),
        )
        return _row_to_dict(await cursor.fetchone())


async def get_channel_dhashes(channel_db_id: int) -> list[tuple[int, int]]:
    """Return all (fingerprint_id, dhash_int) pairs for a channel.

    Only returns rows that have a non-NULL dhash value (photos).
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, dhash FROM media_fingerprints WHERE channel_id = ? AND dhash IS NOT NULL",
            (channel_db_id,),
        )
        rows = await cursor.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]  # type: ignore[index]


async def get_fingerprint(fingerprint_id: int) -> dict[str, Any] | None:
    """Get a single fingerprint by its ID."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM media_fingerprints WHERE id = ?", (fingerprint_id,)
        )
        return _row_to_dict(await cursor.fetchone())


async def add_fingerprints_bulk(channel_db_id: int, items: list[dict[str, Any]]) -> list[int]:
    """Insert multiple fingerprints and return their IDs.

    Each item dict should have keys: file_unique_id, dhash, file_id,
    media_type, queued_post_id.
    """
    if not items:
        return []

    ids: list[int] = []
    async with transaction() as db:
        for item in items:
            cursor = await db.execute(
                """
                INSERT INTO media_fingerprints
                    (channel_id, file_unique_id, dhash, file_id, media_type, queued_post_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_db_id,
                    item.get("file_unique_id"),
                    item.get("dhash"),
                    item.get("file_id"),
                    item.get("media_type"),
                    item.get("queued_post_id"),
                ),
            )
            ids.append(cursor.lastrowid)  # type: ignore[arg-type]
    return ids


async def _mark_fingerprint_posted_in_tx(db, queued_post_id: int) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for mark_fingerprint_posted. Caller owns the tx."""
    await db.execute(
        """
        UPDATE media_fingerprints
        SET posted_at = CURRENT_TIMESTAMP
        WHERE queued_post_id = ?
        """,
        (queued_post_id,),
    )


async def mark_fingerprint_posted(queued_post_id: int) -> None:
    """Set posted_at on fingerprints linked to a queued post."""
    async with transaction() as db:
        await _mark_fingerprint_posted_in_tx(db, queued_post_id)


async def _delete_unposted_fingerprints_in_tx(db, queued_post_id: int) -> None:  # type: ignore[no-untyped-def]
    """In-transaction body for delete_unposted_fingerprints. Caller owns the tx."""
    await db.execute(
        """
        DELETE FROM media_fingerprints
        WHERE queued_post_id = ? AND posted_at IS NULL
        """,
        (queued_post_id,),
    )


async def delete_unposted_fingerprints(queued_post_id: int) -> None:
    """Delete fingerprints for a queued post that was never posted."""
    async with transaction() as db:
        await _delete_unposted_fingerprints_in_tx(db, queued_post_id)


async def remove_staging_item_by_position(user_id: int, position: int) -> None:
    """Remove a single staging item by position and compact remaining positions."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM bulk_staging WHERE user_id = ? AND position = ?",
            (user_id, position),
        )
        await db.execute(
            """
            UPDATE bulk_staging SET position = position - 1
            WHERE user_id = ? AND position > ?
            """,
            (user_id, position),
        )


async def remove_staging_items_by_file_id(user_id: int, file_id: str) -> None:
    """Remove staging items matching a specific file_id."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM bulk_staging WHERE user_id = ? AND file_id = ?",
            (user_id, file_id),
        )


# --- Ownership-checked channel lookups ------------------------------------


async def get_channel_by_telegram_id_for_user(
    user_id: int, telegram_channel_id: str
) -> dict[str, Any] | None:
    channel = await get_channel_by_telegram_id(telegram_channel_id)
    if channel is None or int(channel["user_id"]) != int(user_id):
        return None
    return channel


async def get_channel_by_id_for_user(user_id: int, channel_db_id: int) -> dict[str, Any] | None:
    channel = await get_channel_by_id(channel_db_id)
    if channel is None or int(channel["user_id"]) != int(user_id):
        return None
    return channel
