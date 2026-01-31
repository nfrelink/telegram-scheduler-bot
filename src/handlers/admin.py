"""Admin-only commands: /debug, /stats, /broadcast."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegram import Message, MessageEntity, Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record, get_admin_user_id

logger = logging.getLogger(__name__)


def _utf16_len(text: str) -> int:
    # Telegram entity offsets/lengths are in UTF-16 code units.
    return len(text.encode("utf-16-le")) // 2


def _extract_command_payload(message: Message) -> tuple[str | None, list[MessageEntity] | None]:
    """Extract payload text/entities for '/broadcast <message>'."""
    text = message.text or ""
    if not text.strip():
        return None, None

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None, None

    # Preserve message as-is after the first whitespace following the command token.
    command_token = parts[0]
    start_idx = len(command_token)
    # Skip exactly one whitespace after the command token if present.
    if start_idx < len(text) and text[start_idx].isspace():
        start_idx += 1

    payload_text = text[start_idx:]
    if not payload_text.strip():
        return None, None

    # Adjust entities to new offsets relative to payload_text.
    prefix_text = text[:start_idx]
    prefix_utf16 = _utf16_len(prefix_text)

    entities: list[MessageEntity] = []
    for ent in (message.entities or []):
        ent_end = ent.offset + ent.length
        if ent_end <= prefix_utf16:
            continue
        if ent.offset < prefix_utf16:
            # Entity overlaps the prefix; ignore (usually the command entity).
            continue
        entities.append(
            MessageEntity(
                type=ent.type,
                offset=ent.offset - prefix_utf16,
                length=ent.length,
                url=ent.url,
                user=ent.user,
                language=ent.language,
                custom_emoji_id=getattr(ent, "custom_emoji_id", None),
            )
        )

    return payload_text, (entities or None)


def _get_uptime_seconds(context: ContextTypes.DEFAULT_TYPE) -> float:
    key = "process_started_monotonic"
    started = context.application.bot_data.get(key)
    if not isinstance(started, (int, float)):
        started = time.monotonic()
        context.application.bot_data[key] = started
    return max(0.0, time.monotonic() - float(started))


def _format_uptime(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m {secs}s"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _is_admin(update: Update) -> bool:
    admin_id = get_admin_user_id()
    user_id = update.effective_user.id if update.effective_user else None
    return admin_id is not None and user_id == admin_id


def admin_only(func):  # type: ignore[no-untyped-def]
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await ensure_user_record(update, context)
        if update.message is None:
            return
        if not _is_admin(update):
            await update.message.reply_text("This command is restricted to the bot administrator.")
            logger.warning("Unauthorized admin command attempt by user_id=%s", update.effective_user.id if update.effective_user else None)
            return
        return await func(update, context)

    return wrapper


@admin_only
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    stats = await db.get_system_stats()
    schedule_states = await db.get_schedule_state_counts()

    now = datetime.now(timezone.utc)
    active_since = now - timedelta(days=90)
    active_users = await db.get_active_user_count(since=active_since)

    db_path = os.getenv("DATABASE_PATH", "data/scheduler.db")
    check_interval = int(os.getenv("SCHEDULER_CHECK_INTERVAL", "60") or "60")

    uptime = _format_uptime(_get_uptime_seconds(context))

    msg = (
        "System status (UTC)\n"
        f"- Uptime: {uptime}\n"
        f"- Database: {db_path}\n"
        f"- Scheduler check interval: {check_interval}s\n"
        "\n"
        "Counts\n"
        f"- Users (total): {stats['total_users']}\n"
        f"- Users (active 90d): {active_users}\n"
        f"- Channels (active): {stats['total_channels']}\n"
        f"- Schedules: active={schedule_states.get('active', 0)}, paused={schedule_states.get('paused', 0)}, empty_paused={schedule_states.get('empty_paused', 0)}\n"
        f"- Queued posts: {stats['queued_posts']}\n"
        f"- Failed queued posts (retry_count>0): {stats['failed_posts']}\n"
    )
    await update.message.reply_text(msg)


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    now = datetime.now(timezone.utc)
    active_since = now - timedelta(days=90)

    active_users = await db.get_active_user_count(since=active_since)
    stats = await db.get_system_stats()

    today = now.date()
    last_7_days = today - timedelta(days=6)
    delivery_today = await db.get_delivery_stats_sum_since(since_day=today)
    delivery_7d = await db.get_delivery_stats_sum_since(since_day=last_7_days)

    msg = (
        "Statistics (UTC)\n"
        "\n"
        f"- Active users (90d): {active_users}\n"
        f"- Active channels: {stats['total_channels']}\n"
        f"- Queued posts (global): {stats['queued_posts']}\n"
        f"- Failed queued posts (retry_count>0): {stats['failed_posts']}\n"
        "\n"
        f"- Sent today: {delivery_today['posts_sent']}\n"
        f"- Send failures today: {delivery_today['send_failures']}\n"
        f"- Sent last 7d: {delivery_7d['posts_sent']}\n"
        f"- Send failures last 7d: {delivery_7d['send_failures']}\n"
    )
    await update.message.reply_text(msg)


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    payload_text, payload_entities = _extract_command_payload(update.message)
    if not payload_text:
        await update.message.reply_text(
            "Usage: /broadcast <message>\n"
            "Sends a message to users active in the last 90 days."
        )
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=90)
    users = await db.get_active_users(since=since)

    await update.message.reply_text(
        f"Broadcasting to {len(users)} users (active since {since.date().isoformat()} UTC)..."
    )

    ok = 0
    failed = 0
    for u in users:
        user_id = int(u["id"])
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=payload_text,
                entities=payload_entities,
            )
            ok += 1
        except Exception as e:
            failed += 1
            logger.error("Broadcast failed for user_id=%s: %s", user_id, e, exc_info=True)
        await asyncio.sleep(0.05)

    await update.message.reply_text(f"Broadcast complete. Success: {ok}. Failed: {failed}.")

