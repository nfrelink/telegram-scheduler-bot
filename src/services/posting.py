"""Posting service.

Owns the lifecycle of queued posts (insert / send-completion / retry / cancel
/ pin) and the atomic transactions that span the queued_posts,
media_fingerprints, schedules and delivery_stats tables.

The four send-completion orchestrators (`complete_send`, `complete_retry`,
`complete_failure_pause`, `cancel`) live here rather than in `database.queries`
because they are domain operations, not data access. Their `_xxx_in_tx`
building blocks remain in `database.queries`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from database import queries as db
from database.connection import transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_bulk(
    schedule_id: int,
    *,
    posts: list[dict[str, Any]],
    fingerprints: list[dict[str, Any]] | None = None,
    channel_db_id: int | None = None,
) -> tuple[int, list[int]]:
    """Insert posts and (optionally) their dedup fingerprints.

    `fingerprints` items are matched to inserted posts by `file_id` and
    stamped with `queued_post_id` before insert; items whose `file_id` does
    not match any inserted post (e.g. native forwards without a file_id)
    are still inserted, with `queued_post_id = None`.

    Posts and fingerprints are inserted in two sequential transactions; a
    crash between them leaves posts in the queue without their fingerprint
    rows.

    Returns `(inserted_count, post_ids)`.
    """
    inserted, post_ids = await db.add_queued_posts_bulk(schedule_id, posts)
    if fingerprints and channel_db_id is not None:
        file_id_to_post_id: dict[str, int] = {}
        for post_dict, pid in zip(posts, post_ids):
            fid = post_dict.get("file_id")
            if fid:
                file_id_to_post_id[fid] = pid
        for fp in fingerprints:
            fp["queued_post_id"] = file_id_to_post_id.get(fp.get("file_id", ""))
        await db.add_fingerprints_bulk(int(channel_db_id), fingerprints)
    return inserted, post_ids


# ---------------------------------------------------------------------------
# Schedule attachment for queued posts (catch-up / retry)
# ---------------------------------------------------------------------------


async def bulk_set_scheduled_for(post_updates: list[tuple[int, datetime]]) -> None:
    """Set `scheduled_for` for multiple posts in one transaction."""
    await db.bulk_update_posts_scheduled_for(post_updates)


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


async def pin(post_id: int, *, pinned_at: datetime, user_id: int) -> None:
    """Pin a queued post to a specific send datetime."""
    await db.set_post_pinned_at(post_id, pinned_at, user_id=user_id)


async def unpin(post_id: int, *, user_id: int) -> None:
    """Remove the pinned_at datetime from a queued post, returning it to FIFO."""
    await db.clear_post_pinned_at(post_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Send completion orchestrators (atomic)
# ---------------------------------------------------------------------------


async def complete_send(
    *,
    post_id: int,
    schedule_id: int,
    owner_user_id: int,
    day: date,
    next_planned_run_at: datetime | None,
) -> None:
    """Atomically apply all post-success state mutations.

    Combines: increment posts_sent, mark fingerprints posted, delete the
    queued post (with FIFO position compaction), bump
    `schedule.last_run_at`, and advance `schedule.next_planned_run_at` to
    the freshly-computed next slot.

    `next_planned_run_at` is computed by the caller (engine) immediately
    before invocation; passing it in keeps the recompute outside the tx
    (cheap, pure) while keeping the persisted state mutation atomic with
    the rest.
    """
    async with transaction() as conn:
        await db._increment_delivery_stats_daily_in_tx(conn, day=day, posts_sent_delta=1)
        await db._mark_fingerprint_posted_in_tx(conn, post_id)
        await db._delete_queued_post_in_tx(conn, post_id, user_id=owner_user_id)
        await db._update_schedule_last_run_in_tx(conn, schedule_id)
        await db._update_schedule_next_planned_run_in_tx(conn, schedule_id, next_planned_run_at)


async def complete_retry(
    *,
    post_id: int,
    retry_count: int,
    scheduled_for: datetime,
    day: date,
) -> None:
    """Atomically record a delivery failure and reschedule for retry."""
    async with transaction() as conn:
        await db._increment_delivery_stats_daily_in_tx(conn, day=day, send_failures_delta=1)
        await db._update_post_retry_in_tx(
            conn, post_id, retry_count=retry_count, scheduled_for=scheduled_for
        )


async def complete_failure_pause(
    *,
    schedule_id: int,
    owner_user_id: int,
    day: date,
) -> None:
    """Atomically record the final delivery failure and pause the schedule.

    The post itself is left in the queue with an exhausted retry_count so
    the user can inspect/delete it before resuming. Pausing via
    `_update_schedule_state_in_tx` self-clears `next_planned_run_at`, so no
    separate recompute is needed.
    """
    async with transaction() as conn:
        await db._increment_delivery_stats_daily_in_tx(conn, day=day, send_failures_delta=1)
        await db._update_schedule_state_in_tx(conn, schedule_id, "paused", user_id=owner_user_id)


async def cancel(*, post_id: int, user_id: int) -> None:
    """Atomically remove a queued post and any unposted fingerprints linked
    to it.

    Callers must verify ownership upstream (e.g. via
    `db.get_queued_post_with_owner`); `_delete_queued_post_in_tx` re-checks
    via JOIN as defence-in-depth, but `_delete_unposted_fingerprints_in_tx`
    does not, so a hostile direct call here would still drop the
    fingerprint rows.
    """
    async with transaction() as conn:
        await db._delete_unposted_fingerprints_in_tx(conn, post_id)
        await db._delete_queued_post_in_tx(conn, post_id, user_id=user_id)
