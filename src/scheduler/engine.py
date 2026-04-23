"""Background scheduler loop for executing posting schedules."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
import os
from datetime import datetime, timedelta, timezone

from telegram.ext import ExtBot

from database import queries as db
from database.time import parse_timestamp
from scheduler.executor import send_post
from scheduler.rate_limiter import RateLimiter
from scheduler.timing import calculate_next_run, validate_schedule_pattern
from services import notifications, posting, scheduling
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

CATCHUP_SPACING_SECONDS = int(os.getenv("CATCHUP_SPACING_SECONDS", "30"))
CATCHUP_MAX_RUNS_PER_SCHEDULE = int(os.getenv("CATCHUP_MAX_RUNS_PER_SCHEDULE", "5"))
CATCHUP_MAX_ITERATIONS = 5000

# Heartbeat: if any active schedule exists but no active schedule has fired
# within this window, the loop is presumed wedged and the admin gets pinged.
# Default of 36h is generous: a once-a-day schedule (cron-style) can lapse a
# whole day without false-positive, but a multi-times-per-day schedule going
# silent for >36h is almost certainly broken.
HEARTBEAT_MAX_HOURS = int(os.getenv("HEARTBEAT_MAX_HOURS", "36"))


async def start_scheduler(bot: ExtBot) -> None:  # pragma: no cover
    """Run the scheduler loop until cancelled.

    Bootstrap glue around the (heavily covered) _process_due_schedules,
    _get_sleep_seconds and _catch_up_missed_posts helpers; not unit-tested
    directly because it is an unbounded asyncio loop driven by real wall-clock.
    """
    check_interval = int(os.getenv("SCHEDULER_CHECK_INTERVAL", "60") or "60")
    check_interval = max(1, check_interval)

    rate_limiter = RateLimiter(min_interval_seconds=3.0)

    logger.info(
        "Scheduler started (check interval: %ss)",
        check_interval,
        extra={"event": "scheduler_started", "check_interval_seconds": check_interval},
    )
    await _catch_up_missed_posts()

    try:
        while True:
            try:
                await _process_due_schedules(bot, rate_limiter=rate_limiter)
            except Exception as e:
                logger.error(
                    "Error in scheduler tick: %s",
                    e,
                    exc_info=True,
                    extra={"event": "scheduler_tick_error"},
                )

            sleep_seconds = await _get_sleep_seconds(check_interval)
            await asyncio.sleep(sleep_seconds)
    except asyncio.CancelledError:
        logger.info("Scheduler cancelled", extra={"event": "scheduler_cancelled"})
        raise


async def _get_sleep_seconds(default_seconds: int) -> float:
    """Choose a sleep interval, respecting upcoming scheduled_for and pinned_at times."""
    now = datetime.now(timezone.utc)
    earliest: datetime | None = None

    for getter in (db.get_earliest_scheduled_for, db.get_earliest_pinned_at):
        try:
            raw = await getter()
            dt = parse_timestamp(raw)
        except Exception:
            dt = None
        if dt is not None and (earliest is None or dt < earliest):
            earliest = dt

    if earliest is None:
        return float(default_seconds)

    delta = (earliest - now).total_seconds()
    if delta <= 0:
        return 1.0

    return float(min(default_seconds, max(1.0, delta)))


async def _catch_up_missed_posts() -> None:
    """On startup, schedule a small burst of missed posts for near-future execution.

    Behaviour, per active schedule:
      1. Determine the cursor: prefer `next_planned_run_at`. If NULL (e.g. the
         column was just added by migration, or the schedule was created before
         next_planned_run_at became canonical), derive a cursor from
         `last_run_at` or `created_at` so we have a starting point.
      2. Walk forward via `calculate_next_run`, counting slots strictly <= now.
         Stop at CATCHUP_MAX_RUNS_PER_SCHEDULE (default 5) — that's the cap on
         the catch-up *burst*, not on the queue: any remaining queued posts
         continue to drain through the regular tick at the schedule's normal
         pattern cadence.
      3. Assign `scheduled_for = now + i*CATCHUP_SPACING_SECONDS` to the first N
         unscheduled queued posts (FIFO order, preserved by `position`).
      4. Persist `next_planned_run_at = calculate_next_run(after=now)` so the
         regular tick resumes at the next legitimate slot.

    Skips the schedule entirely if there's nothing to catch up; in that case
    we still backfill `next_planned_run_at` if it was NULL, to satisfy the tick's
    invariant.
    """
    now = datetime.now(timezone.utc)
    schedules = await db.get_active_schedules()

    total_scheduled = 0

    for schedule in schedules:
        schedule_id = int(schedule["id"])
        try:
            cursor = _catchup_cursor(schedule, now=now)
            if cursor is None:
                # Nothing reasonable to compute from (no NPR, no last_run_at,
                # no created_at, no valid pattern). Leave it; the tick's
                # defensive backfill will handle it.
                continue

            missed = 0
            # CATCHUP_MAX_ITERATIONS is a hard upper bound that protects against
            # a pattern that could (pathologically) keep returning timestamps
            # <= now forever. With the default CATCHUP_MAX_RUNS_PER_SCHEDULE=5
            # the inner cap break fires first; the loop "ran to completion"
            # branch is only exercisable by setting MAX < cap, which is not a
            # supported configuration.
            for _ in range(CATCHUP_MAX_ITERATIONS):
                if cursor > now:
                    break
                missed += 1
                if missed >= CATCHUP_MAX_RUNS_PER_SCHEDULE:
                    cursor = calculate_next_run(schedule, after=cursor)
                    break
                cursor = calculate_next_run(schedule, after=cursor)

            # Always normalise next_planned_run_at to the next slot strictly
            # after `now`. This both backfills NULL on first migration tick
            # and prevents the regular tick from chewing through past slots.
            new_next_planned = calculate_next_run(schedule, after=now)
            await scheduling.persist_next_run(schedule_id, new_next_planned)

            if missed <= 0:
                continue

            candidates = await db.get_queued_posts_unscheduled(
                schedule_id, limit=missed
            )
            if not candidates:
                continue

            updates: list[tuple[int, datetime]] = []
            for i, post in enumerate(candidates[:missed]):
                post_id = int(post["id"])
                updates.append(
                    (post_id, now + timedelta(seconds=CATCHUP_SPACING_SECONDS * i))
                )

            await posting.bulk_set_scheduled_for(updates)
            total_scheduled += len(updates)

            logger.info(
                "Catch-up scheduled %s posts for schedule id=%s; next_planned_run_at=%s",
                len(updates),
                schedule_id,
                new_next_planned.isoformat(),
                extra={
                    "event": "catchup_scheduled",
                    "schedule_id": schedule_id,
                    "scheduled_count": len(updates),
                    "next_planned_run_at": new_next_planned.isoformat(),
                },
            )
        except Exception as e:
            logger.error(
                "Catch-up failed for schedule id=%s: %s",
                schedule_id,
                e,
                exc_info=True,
                extra={"event": "catchup_failed", "schedule_id": schedule_id},
            )

    if total_scheduled:
        logger.info(
            "Catch-up scheduled %s posts total",
            total_scheduled,
            extra={"event": "catchup_summary", "total_scheduled": total_scheduled},
        )


def _catchup_cursor(schedule: dict[str, Any], *, now: datetime) -> datetime | None:
    """Pick the timestamp to start counting missed slots from.

    Prefers next_planned_run_at; falls back to deriving a value from the
    historical last_run_at / created_at for schedules predating the column.
    Returns None if no reasonable value can be derived.
    """
    npa = parse_timestamp(schedule.get("next_planned_run_at"))
    if npa is not None:
        return npa

    base = parse_timestamp(schedule.get("last_run_at")) or parse_timestamp(
        schedule.get("created_at")
    )
    if base is None:
        return None
    try:
        return calculate_next_run(schedule, after=base)
    except ValueError:
        return None


async def _process_due_schedules(bot: ExtBot, *, rate_limiter: RateLimiter) -> None:
    now = datetime.now(timezone.utc)
    schedules = await db.get_active_schedules()

    for schedule in schedules:
        try:
            await _process_schedule(bot, schedule, now=now, rate_limiter=rate_limiter)
        except Exception as e:
            logger.error(
                "Error processing schedule id=%s: %s",
                schedule.get("id"),
                e,
                exc_info=True,
                extra={
                    "event": "schedule_tick_error",
                    "schedule_id": schedule.get("id"),
                },
            )

    await _heartbeat_check(bot, now=now, active_count=len(schedules))


async def _heartbeat_check(bot: ExtBot, *, now: datetime, active_count: int) -> None:
    """If any schedules are active but none has fired in `HEARTBEAT_MAX_HOURS`,
    DM the admin. No-op when no schedules are active (silence is correct
    when nothing is supposed to fire).

    Debounced under a single global key, so a wedged loop pings once per
    debounce window rather than once per tick.
    """
    if active_count <= 0:
        return

    latest = await db.get_latest_active_schedule_run_at()
    if latest is None:
        # Active schedules exist but none have ever fired. Could be a
        # genuinely fresh deploy; rather than ping the admin on first boot,
        # let last_run_at populate naturally and report on the next tick if
        # it stays empty long enough.
        return

    age = now - latest
    if age <= timedelta(hours=HEARTBEAT_MAX_HOURS):
        return

    await notifications.notify_admin(
        bot,
        event="scheduler_heartbeat_stalled",
        lines=[
            ("active_schedules", active_count),
            ("latest_last_run_at", latest.isoformat()),
            ("age_hours", round(age.total_seconds() / 3600, 1)),
            ("threshold_hours", HEARTBEAT_MAX_HOURS),
        ],
        debounce_key="scheduler_heartbeat_stalled",
    )


async def _process_schedule(
    bot: ExtBot,
    schedule: dict[str, Any],
    *,
    now: datetime,
    rate_limiter: RateLimiter,
) -> None:
    schedule_id = int(schedule["id"])
    telegram_channel_id = str(schedule["telegram_channel_id"])
    owner_user_id = int(schedule["owner_user_id"])
    schedule_name = schedule.get("name") or f"Schedule {schedule_id}"
    channel_name = schedule.get("channel_name") or telegram_channel_id

    ok, reason = validate_schedule_pattern(schedule.get("pattern") or {})
    if not ok:
        await scheduling.pause(schedule_id, user_id=owner_user_id)
        await _notify_user(
            bot,
            owner_user_id,
            *render(
                [
                    Segment("Schedule '"),
                    Segment(schedule_name),
                    Segment("' for channel '"),
                    Segment(channel_name),
                    Segment("' was paused because its pattern is invalid.\n"),
                    Segment("Reason: "),
                    Segment(str(reason)),
                    Segment("\nUse /schedules to fix or delete it."),
                ]
            ),
        )
        logger.warning(
            "Paused schedule id=%s due to invalid pattern: %s",
            schedule_id,
            reason,
            extra={
                "event": "schedule_paused_invalid_pattern",
                "schedule_id": schedule_id,
                "channel_id": telegram_channel_id,
                "owner_user_id": owner_user_id,
                "reason": reason,
            },
        )
        queue_depth = await db.count_queued_posts(schedule_id)
        await notifications.notify_admin(
            bot,
            event="schedule_paused_invalid_pattern",
            lines=[
                ("schedule_id", schedule_id),
                ("schedule_name", schedule_name),
                ("channel_id", telegram_channel_id),
                ("channel_name", channel_name),
                ("owner_user_id", owner_user_id),
                ("reason", reason),
                ("queue_depth", queue_depth),
            ],
            debounce_key=f"schedule_paused_invalid_pattern:{schedule_id}",
        )
        return

    post = await db.get_next_queued_post(schedule_id, now=now)
    if post is None:
        await _handle_empty_queue(
            bot,
            schedule=schedule,
            owner_user_id=owner_user_id,
        )
        return

    post_id = int(post["id"])
    pinned_at = parse_timestamp(post.get("pinned_at"))
    scheduled_for = parse_timestamp(post.get("scheduled_for"))

    if pinned_at is not None:
        # pinned_at <= now is guaranteed by get_next_queued_post — fire immediately.
        pass
    elif scheduled_for is not None:
        # Catch-up or retry post: send only if its scheduled_for has passed.
        if scheduled_for > now:
            return
    else:
        # Normal FIFO post: gate on next_planned_run_at, the single source of
        # truth maintained by recompute_next_run / complete_post_send / catchup.
        npa = parse_timestamp(schedule.get("next_planned_run_at"))
        if npa is None:
            # Defensive backfill for an active schedule with a NULL value
            # (e.g. created before the column existed and never caught up).
            # We persist immediately so the next tick has a consistent value.
            npa = calculate_next_run(schedule, after=now)
            await scheduling.persist_next_run(schedule_id, npa)
        if now < npa:
            return

    await rate_limiter.wait_if_needed(telegram_channel_id)
    ok, error_text = await send_post(
        bot, telegram_channel_id=telegram_channel_id, post=post
    )

    if ok:
        # Compute the next planned slot from `now`, not from the slot we just
        # fired. For daily/weekly this picks the next clock time after the
        # actual fire; for interval it preserves cadence relative to fires.
        new_next_planned = calculate_next_run(schedule, after=now)
        await posting.complete_send(
            post_id=post_id,
            schedule_id=schedule_id,
            owner_user_id=owner_user_id,
            day=now.date(),
            next_planned_run_at=new_next_planned,
        )
        logger.info(
            "Sent post id=%s for schedule id=%s to channel=%s",
            post_id,
            schedule_id,
            telegram_channel_id,
            extra={
                "event": "post_sent",
                "schedule_id": schedule_id,
                "post_id": post_id,
                "channel_id": telegram_channel_id,
                "owner_user_id": owner_user_id,
                "next_planned_run_at": new_next_planned.isoformat(),
            },
        )
        return

    await _handle_post_failure(
        bot,
        schedule=schedule,
        post=post,
        owner_user_id=owner_user_id,
        now=now,
        error_text=error_text,
    )


async def _handle_empty_queue(
    bot: ExtBot, *, schedule: dict[str, Any], owner_user_id: int
) -> None:
    schedule_id = int(schedule["id"])

    # Avoid spamming: transition to empty_paused.
    await scheduling.mark_empty(schedule_id, user_id=owner_user_id)

    channel_name = (
        schedule.get("channel_name") or schedule.get("telegram_channel_id") or "channel"
    )
    schedule_name = schedule.get("name") or f"Schedule {schedule_id}"

    await _notify_user(
        bot,
        owner_user_id,
        *render(
            [
                Segment("Schedule '"),
                Segment(schedule_name),
                Segment("' for channel '"),
                Segment(channel_name),
                Segment("' was paused because the queue is empty.\n"),
                Segment("Add posts with /bulk, then use /schedules to resume."),
            ]
        ),
    )


async def _handle_post_failure(
    bot: ExtBot,
    *,
    schedule: dict[str, Any],
    post: dict[str, Any],
    owner_user_id: int,
    now: datetime,
    error_text: str | None = None,
) -> None:
    post_id = int(post["id"])
    schedule_id = int(post["schedule_id"])

    retry_count = int(post.get("retry_count") or 0) + 1

    if retry_count <= MAX_RETRIES:
        delay_minutes = 2**retry_count  # 2, 4, 8
        retry_time = now + timedelta(minutes=delay_minutes)
        await posting.complete_retry(
            post_id=post_id,
            retry_count=retry_count,
            scheduled_for=retry_time,
            day=now.date(),
        )
        logger.warning(
            "Post id=%s failed (retry %s/%s) scheduled for %s",
            post_id,
            retry_count,
            MAX_RETRIES,
            retry_time.isoformat(),
            extra={
                "event": "post_send_retry",
                "schedule_id": schedule_id,
                "post_id": post_id,
                "retry_count": retry_count,
                "max_retries": MAX_RETRIES,
                "scheduled_for": retry_time.isoformat(),
                "last_error": error_text,
            },
        )
        return

    # Stop the schedule to avoid repeated failures/spam; user can delete the post and resume.
    await posting.complete_failure_pause(
        schedule_id=schedule_id,
        owner_user_id=owner_user_id,
        day=now.date(),
    )

    telegram_channel_id = schedule.get("telegram_channel_id") or "(unknown)"
    channel_name = schedule.get("channel_name") or telegram_channel_id or "channel"
    schedule_name = schedule.get("name") or f"Schedule {schedule_id}"

    await _notify_user(
        bot,
        owner_user_id,
        *render(
            [
                Segment("Posting failed for schedule '"),
                Segment(schedule_name),
                Segment("' (channel '"),
                Segment(channel_name),
                Segment(f"') after {MAX_RETRIES} attempts.\n"),
                Segment("Post ID: "),
                Segment(str(post_id), code=True),
                Segment("\nThe schedule has been paused.\nUse /deletepost "),
                Segment(str(post_id), code=True),
                Segment(" to remove the post, then use /schedules to resume."),
            ]
        ),
    )

    queue_depth = await db.count_queued_posts(schedule_id)
    logger.error(
        "Paused schedule id=%s after %s failed send attempts on post id=%s: %s",
        schedule_id,
        retry_count,
        post_id,
        error_text or "(unavailable)",
        extra={
            "event": "schedule_paused_send_failure",
            "schedule_id": schedule_id,
            "channel_id": telegram_channel_id,
            "owner_user_id": owner_user_id,
            "post_id": post_id,
            "retry_count": retry_count,
            "last_error": error_text,
            "queue_depth": queue_depth,
        },
    )
    await notifications.notify_admin(
        bot,
        event="schedule_paused_send_failure",
        lines=[
            ("schedule_id", schedule_id),
            ("schedule_name", schedule_name),
            ("channel_id", telegram_channel_id),
            ("channel_name", channel_name),
            ("owner_user_id", owner_user_id),
            ("post_id", post_id),
            ("retry_count", retry_count),
            ("last_error", error_text or "(unavailable)"),
            ("queue_depth", queue_depth),
        ],
        debounce_key=f"schedule_paused_send_failure:{schedule_id}",
    )


async def _notify_user(bot: ExtBot, user_id: int, message: str, entities) -> None:  # type: ignore[no-untyped-def]
    try:
        await bot.send_message(chat_id=user_id, text=message, entities=entities)
    except Exception as e:
        logger.error(
            "Failed to notify user %s: %s",
            user_id,
            e,
            exc_info=True,
            extra={"event": "user_notify_failed", "user_id": user_id},
        )
