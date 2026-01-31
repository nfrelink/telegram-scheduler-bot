"""Background scheduler loop for executing posting schedules."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram.ext import ExtBot

from database import queries as db
from scheduler.executor import send_post
from scheduler.rate_limiter import RateLimiter
from scheduler.timing import calculate_next_run, validate_schedule_pattern
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

CATCHUP_SPACING_SECONDS = 10
CATCHUP_MAX_RUNS_PER_SCHEDULE = 20
CATCHUP_MAX_ITERATIONS = 5000

def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        # SQLite CURRENT_TIMESTAMP -> "YYYY-MM-DD HH:MM:SS"
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def start_scheduler(bot: ExtBot) -> None:
    """Run the scheduler loop until cancelled."""
    check_interval = int(os.getenv("SCHEDULER_CHECK_INTERVAL", "60") or "60")
    check_interval = max(1, check_interval)

    rate_limiter = RateLimiter(min_interval_seconds=3.0)

    logger.info("Scheduler started (check interval: %ss)", check_interval)
    await _catch_up_missed_posts()

    try:
        while True:
            try:
                await _process_due_schedules(bot, rate_limiter=rate_limiter)
            except Exception as e:
                logger.error("Error in scheduler tick: %s", e, exc_info=True)

            sleep_seconds = await _get_sleep_seconds(check_interval)
            await asyncio.sleep(sleep_seconds)
    except asyncio.CancelledError:
        logger.info("Scheduler cancelled")
        raise


async def _get_sleep_seconds(default_seconds: int) -> float:
    """Choose a sleep interval, respecting upcoming scheduled_for times."""
    try:
        earliest_raw = await db.get_earliest_scheduled_for()
        earliest = _parse_timestamp(earliest_raw)
    except Exception:
        earliest = None

    if earliest is None:
        return float(default_seconds)

    now = datetime.now(timezone.utc)
    delta = (earliest - now).total_seconds()
    if delta <= 0:
        # There is at least one due scheduled_for; loop again soon.
        return 1.0

    return float(min(default_seconds, max(1.0, delta)))


async def _catch_up_missed_posts() -> None:
    """On startup, schedule missed posts for near-future execution.

    This sets queued_posts.scheduled_for for the first N unscheduled queued posts per schedule.
    The scheduler loop will then wake up in time to process these posts.
    """
    now = datetime.now(timezone.utc)
    schedules = await db.get_active_schedules()

    total_scheduled = 0

    for schedule in schedules:
        schedule_id = int(schedule["id"])
        try:
            last_run_at = _parse_timestamp(schedule.get("last_run_at"))
            created_at = _parse_timestamp(schedule.get("created_at"))
            base_after = last_run_at or created_at
            if base_after is None:
                continue

            cursor = base_after
            missed = 0

            for _ in range(CATCHUP_MAX_ITERATIONS):
                next_run = calculate_next_run(schedule, after=cursor)
                if next_run <= now:
                    missed += 1
                    cursor = next_run
                    if missed >= CATCHUP_MAX_RUNS_PER_SCHEDULE:
                        break
                else:
                    break

            if missed <= 0:
                continue

            candidates = await db.get_queued_posts_unscheduled(schedule_id, limit=missed)
            if not candidates:
                continue

            updates: list[tuple[int, datetime]] = []
            base_time = now
            for i, post in enumerate(candidates[:missed]):
                post_id = int(post["id"])
                updates.append((post_id, base_time + timedelta(seconds=CATCHUP_SPACING_SECONDS * i)))

            await db.bulk_update_posts_scheduled_for(updates)
            total_scheduled += len(updates)

            logger.info(
                "Catch-up scheduled %s posts for schedule id=%s",
                len(updates),
                schedule_id,
            )
        except Exception as e:
            logger.error("Catch-up failed for schedule id=%s: %s", schedule_id, e, exc_info=True)

    if total_scheduled:
        logger.info("Catch-up scheduled %s posts total", total_scheduled)


async def _process_due_schedules(bot: ExtBot, *, rate_limiter: RateLimiter) -> None:
    now = datetime.now(timezone.utc)
    schedules = await db.get_active_schedules()

    for schedule in schedules:
        try:
            await _process_schedule(bot, schedule, now=now, rate_limiter=rate_limiter)
        except Exception as e:
            logger.error("Error processing schedule id=%s: %s", schedule.get("id"), e, exc_info=True)


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
        await db.update_schedule_state(schedule_id, "paused")
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
                    Segment("\nFix it with /editschedule "),
                    Segment(str(schedule_id), code=True),
                    Segment(" or delete it with /deleteschedule "),
                    Segment(str(schedule_id), code=True),
                    Segment("."),
                ]
            ),
        )
        logger.warning("Paused schedule id=%s due to invalid pattern: %s", schedule_id, reason)
        return

    post = await db.get_next_queued_post(schedule_id)
    if post is None:
        await _handle_empty_queue(
            bot,
            schedule=schedule,
            owner_user_id=owner_user_id,
        )
        return

    post_id = int(post["id"])
    scheduled_for = _parse_timestamp(post.get("scheduled_for"))
    if scheduled_for is not None and scheduled_for > now:
        return

    last_run_at = _parse_timestamp(schedule.get("last_run_at"))
    created_at = _parse_timestamp(schedule.get("created_at"))
    base_after = last_run_at or created_at or now

    due_by_pattern = False
    if scheduled_for is None:
        next_run = calculate_next_run(schedule, after=base_after)
        due_by_pattern = now >= next_run

    due = (scheduled_for is not None and scheduled_for <= now) or due_by_pattern
    if not due:
        return

    await rate_limiter.wait_if_needed(telegram_channel_id)
    ok = await send_post(bot, telegram_channel_id=telegram_channel_id, post=post)

    if ok:
        await db.increment_delivery_stats_daily(day=now.date(), posts_sent_delta=1)
        await db.delete_queued_post(post_id)
        await db.update_schedule_last_run(schedule_id)
        return

    await _handle_post_failure(
        bot,
        schedule=schedule,
        post=post,
        owner_user_id=owner_user_id,
        now=now,
    )


async def _handle_empty_queue(bot: ExtBot, *, schedule: dict[str, Any], owner_user_id: int) -> None:
    schedule_id = int(schedule["id"])

    # Avoid spamming: transition to empty_paused.
    await db.update_schedule_state(schedule_id, "empty_paused")

    channel_name = schedule.get("channel_name") or schedule.get("telegram_channel_id") or "channel"
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
                Segment("Add posts with /bulk "),
                Segment(str(schedule_id), code=True),
                Segment(", then resume with /resumeschedule "),
                Segment(str(schedule_id), code=True),
                Segment("."),
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
) -> None:
    post_id = int(post["id"])
    schedule_id = int(post["schedule_id"])

    retry_count = int(post.get("retry_count") or 0) + 1

    await db.increment_delivery_stats_daily(day=now.date(), send_failures_delta=1)

    if retry_count <= MAX_RETRIES:
        delay_minutes = 2 ** retry_count  # 2, 4, 8
        retry_time = now + timedelta(minutes=delay_minutes)
        await db.update_post_retry(post_id, retry_count=retry_count, scheduled_for=retry_time)
        logger.warning(
            "Post id=%s failed (retry %s/%s) scheduled for %s",
            post_id,
            retry_count,
            MAX_RETRIES,
            retry_time.isoformat(),
        )
        return

    # Stop the schedule to avoid repeated failures/spam; user can delete the post and resume.
    await db.update_schedule_state(schedule_id, "paused")

    channel_name = schedule.get("channel_name") or schedule.get("telegram_channel_id") or "channel"
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
                Segment(" to remove the post, then /resumeschedule "),
                Segment(str(schedule_id), code=True),
                Segment("."),
            ]
        ),
    )


async def _notify_user(bot: ExtBot, user_id: int, message: str, entities) -> None:  # type: ignore[no-untyped-def]
    try:
        await bot.send_message(chat_id=user_id, text=message, entities=entities)
    except Exception as e:
        logger.error("Failed to notify user %s: %s", user_id, e, exc_info=True)

