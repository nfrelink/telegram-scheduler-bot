"""Persistent per-user selection context (channel/schedule).

This helps avoid repeatedly copying IDs, and allows commands to default to the
currently selected channel/schedule.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import access as db_access
from database import queries as db
from handlers.common import ensure_user_record
from handlers.verification import resolve_channel_id
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def selection_segments(details: dict) -> list[Segment]:
    channel_name = details.get("channel_name")
    telegram_channel_id = details.get("telegram_channel_id")
    schedule_id = details.get("selected_schedule_id")
    schedule_name = details.get("schedule_name")
    schedule_state = details.get("schedule_state")

    if not telegram_channel_id and not schedule_id:
        return [Segment("Current selection: none")]

    segments: list[Segment] = [Segment("Current selection:\n")]

    if telegram_channel_id:
        segments += [
            Segment("- Channel: "),
            Segment(str(channel_name or telegram_channel_id)),
            Segment(" ("),
            Segment(str(telegram_channel_id), code=True),
            Segment(")\n"),
        ]

    if schedule_id:
        name_part = str(schedule_name or f"Schedule {schedule_id}")
        state_part = f" [{schedule_state}]" if schedule_state else ""
        segments += [
            Segment("- Schedule: "),
            Segment(name_part),
            Segment(" "),
            Segment(str(schedule_id), code=True),
            Segment(state_part),
        ]

    return segments


async def _selection_summary_for_user(user_id: int) -> tuple[str, object | None]:
    details = await db.get_user_context_details(user_id)
    segments = selection_segments(details)
    text, entities = render(segments)
    return text, entities


async def selection_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current selection."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    text, entities = await _selection_summary_for_user(update.effective_user.id)
    await update.message.reply_text(text, entities=entities)


async def clearselection_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear current selection."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    await db.clear_user_context(update.effective_user.id)
    await update.message.reply_text("Selection cleared.")


async def selectchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Select a channel as the default target for channel-level operations."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /selectchannel <@channel or -100...>\n"
            "Tip: use /listchannels to see your verified channels.\n"
            "If you don't know the numeric id yet, post /channelid in the channel after adding me as admin."
        )
        return

    telegram_channel_id = await resolve_channel_id(context, context.args[0])
    if telegram_channel_id is None:
        await update.message.reply_text(
            "Could not resolve that channel.\n"
            "Tip: use /listchannels to copy the numeric id."
        )
        return

    channel = await db_access.get_channel_by_telegram_id_for_user(update.effective_user.id, telegram_channel_id)
    if channel is None:
        await update.message.reply_text("Channel not found or not owned by you.")
        return

    await db.set_user_context(
        user_id=update.effective_user.id,
        selected_channel_id=int(channel["id"]),
        selected_schedule_id=None,
    )

    details = await db.get_user_context_details(update.effective_user.id)
    segments = [Segment("Selected channel.\n\n"), *selection_segments(details)]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def selectschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Select a schedule as the default target for schedule/queue operations."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /selectschedule <schedule_id>\nTip: use /listschedules first.")
        return

    try:
        schedule_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid schedule id.")
        return

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return

    await db.set_user_context(
        user_id=update.effective_user.id,
        selected_channel_id=int(schedule["channel_id"]),
        selected_schedule_id=schedule_id,
    )

    details = await db.get_user_context_details(update.effective_user.id)
    segments = [Segment("Selected schedule.\n\n"), *selection_segments(details)]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)

