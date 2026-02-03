"""Queue inspection and management commands."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from handlers.selection import selection_segments
from scheduler.engine import _parse_timestamp  # reuse parsing helper (internal)
from scheduler.timing import calculate_next_run
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _format_dt(dt: datetime, *, tz_name: str | None = None) -> str:
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    return dt.astimezone(tz).replace(microsecond=0).isoformat()


def _media_group_is_forwarded(media_group_data: object) -> bool:
    if not isinstance(media_group_data, str) or not media_group_data.strip():
        return False
    try:
        items = json.loads(media_group_data)
    except Exception:
        return False
    if not isinstance(items, list) or not items:
        return False
    first = items[0]
    if not isinstance(first, dict):
        return False
    return first.get("forward_from_chat_id") is not None and first.get("forward_from_message_id") is not None


async def view_queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View next N posts in a schedule queue."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args:
        schedule_id = _parse_int(context.args[0])
        if schedule_id is None:
            await update.message.reply_text("Invalid schedule id.")
            return
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /viewqueue <schedule_id> [count]\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
        )
        return

    limit = 10
    if len(context.args) >= 2:
        parsed_limit = _parse_int(context.args[1])
        if parsed_limit is None or parsed_limit <= 0:
            await update.message.reply_text("Invalid count.")
            return
        limit = min(parsed_limit, 50)

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return
    tz_name = str(schedule.get("timezone") or "UTC")

    if not used_selected:
        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(schedule["channel_id"]),
            selected_schedule_id=schedule_id,
        )

    posts = await db.get_queued_posts(schedule_id, limit=limit)
    if not posts:
        details = await db.get_user_context_details(update.effective_user.id)
        msg_text, msg_entities = render(
            [Segment("Queue is empty.\n\n"), *selection_segments(details)]
        )
        await update.message.reply_text(msg_text, entities=msg_entities)
        return

    now = datetime.now(timezone.utc)
    cursor_time = calculate_next_run(schedule, after=now)

    segments: list[Segment] = [
        Segment(f"Next {len(posts)} queued posts for schedule "),
        Segment(str(schedule_id), code=True),
        Segment(f" (times shown in {tz_name}):\n"),
    ]
    for p in posts:
        post_id = p.get("id")
        media_type = p.get("media_type")
        retry_count = p.get("retry_count", 0)

        scheduled_for = _parse_timestamp(p.get("scheduled_for"))
        planned_time = scheduled_for or cursor_time
        cursor_time = calculate_next_run(schedule, after=planned_time)

        caption = (p.get("caption") or "").strip()
        if len(caption) > 40:
            caption = caption[:37] + "..."

        extra = []
        if caption:
            extra.append(f"caption='{caption}'")
        if retry_count:
            extra.append(f"retries={retry_count}")
        if p.get("forward_from_chat_id") is not None and p.get("forward_from_message_id") is not None:
            extra.append("forwarded")
        if media_type == "media_group" and _media_group_is_forwarded(p.get("media_group_data")):
            extra.append("forwarded")

        extra_text = f" ({', '.join(extra)})" if extra else ""

        segments += [
            Segment("- post "),
            Segment(str(post_id), code=True),
            Segment(": "),
            Segment(str(media_type)),
            Segment(" at "),
            Segment(_format_dt(planned_time, tz_name=tz_name)),
            Segment(extra_text),
            Segment("\n"),
        ]

    segments += [Segment("\n"), *selection_segments(await db.get_user_context_details(update.effective_user.id))]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def delete_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a queued post by id."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /deletepost <post_id>")
        return

    post_id = _parse_int(context.args[0])
    if post_id is None:
        await update.message.reply_text("Invalid post id.")
        return

    post = await db.get_queued_post_with_owner(post_id)
    if post is None or int(post["owner_user_id"]) != update.effective_user.id:
        await update.message.reply_text("Post not found or not owned by you.")
        return

    await db.delete_queued_post(post_id)
    text, entities = render([Segment("Post "), Segment(str(post_id), code=True), Segment(" deleted.")])
    await update.message.reply_text(text, entities=entities)


async def test_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simulate the next N schedule runs without posting."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args:
        schedule_id = _parse_int(context.args[0])
        if schedule_id is None:
            await update.message.reply_text("Invalid schedule id.")
            return
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /testschedule <schedule_id> [run_count]\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
        )
        return

    run_count = 5
    if len(context.args) >= 2:
        parsed = _parse_int(context.args[1])
        if parsed is None or parsed <= 0:
            await update.message.reply_text("Invalid run_count.")
            return
        run_count = min(parsed, 20)

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return
    tz_name = str(schedule.get("timezone") or "UTC")

    if not used_selected:
        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(schedule["channel_id"]),
            selected_schedule_id=schedule_id,
        )

    now = datetime.now(timezone.utc)
    cursor_time = now

    segments: list[Segment] = [
        Segment(f"Next {run_count} runs for schedule "),
        Segment(str(schedule_id), code=True),
        Segment(f" (times shown in {tz_name}):\n"),
    ]
    for i in range(run_count):
        cursor_time = calculate_next_run(schedule, after=cursor_time)

        posts = await db.get_queued_posts(schedule_id, limit=1, offset=i)
        post = posts[0] if posts else None

        has_post = post is not None
        segments += [
            Segment(f"- run {i + 1} at "),
            Segment(_format_dt(cursor_time, tz_name=tz_name)),
            Segment(": "),
        ]
        if has_post:
            segments += [
                Segment("post "),
                Segment(str(post.get("id")), code=True),
                Segment(" ("),
                Segment(str(post.get("media_type"))),
                Segment(")"),
            ]
        else:
            segments += [Segment("no post")]
        segments += [Segment("\n")]

    segments += [Segment("\n"), *selection_segments(await db.get_user_context_details(update.effective_user.id))]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


def _unused_for_type_checking(_: Any) -> None:
    # Avoid unused import warnings when type checkers are enabled.
    return

