"""Persistent per-user selection context (channel/schedule).

/select presents an inline keyboard: pick a channel, then a schedule.
The current selection is stored in the users table and shown in confirmations.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

_CB_BACK = "sc:back"
_CB_CHANNEL = "sc:ch:"   # + channel_db_id
_CB_SET = "sc:set:"      # + channel_db_id:schedule_id


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
            Segment("\n"),
        ]

    if schedule_id:
        name_part = str(schedule_name or f"Schedule {schedule_id}")
        state_part = f" [{schedule_state}]" if schedule_state else ""
        segments += [
            Segment("- Schedule: "),
            Segment(name_part),
            Segment(state_part),
        ]

    return segments


async def _channels_keyboard(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the top-level channel list keyboard."""
    channels = await db.get_user_channels(user_id)
    if not channels:
        return "You have no verified channels. Use /channels to add one.", None

    rows = []
    for ch in channels:
        ch_id = int(ch["id"])
        name = str(ch.get("channel_name") or ch.get("telegram_channel_id") or f"Channel {ch_id}")
        count = await db.get_channel_queue_count(ch_id)
        label = f"{name}  ({count} queued)" if count else name
        rows.append([InlineKeyboardButton(label, callback_data=f"{_CB_CHANNEL}{ch_id}")])

    return "Select a channel:", InlineKeyboardMarkup(rows)


async def _schedules_keyboard(channel_db_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the schedule list keyboard for a given channel."""
    schedules = await db.get_channel_schedules(channel_db_id)
    if not schedules:
        return "This channel has no schedules yet.", None

    _state_label = {"active": "active", "paused": "paused", "empty_paused": "empty"}
    rows = []
    for s in schedules:
        s_id = int(s["id"])
        name = str(s.get("name") or f"Schedule {s_id}")
        state = _state_label.get(str(s.get("state") or ""), str(s.get("state") or ""))
        count = await db.get_queue_count(s_id)
        label = f"{name}  •  {state}  •  {count} queued"
        rows.append([InlineKeyboardButton(label, callback_data=f"{_CB_SET}{channel_db_id}:{s_id}")])

    rows.append([InlineKeyboardButton("< Back", callback_data=_CB_BACK)])
    return "Select a schedule:", InlineKeyboardMarkup(rows)


async def select_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/select — show the channel/schedule picker."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    text, keyboard = await _channels_keyboard(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=keyboard)


async def select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sc:* inline keyboard callbacks for /select."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    if data == _CB_BACK:
        text, keyboard = await _channels_keyboard(user_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
        return

    if data.startswith(_CB_CHANNEL):
        try:
            channel_db_id = int(data[len(_CB_CHANNEL):])
        except ValueError:
            return

        # Ownership check.
        channels = await db.get_user_channels(user_id)
        if channel_db_id not in {int(c["id"]) for c in channels}:
            await query.answer("Channel not found.", show_alert=True)
            return

        text, keyboard = await _schedules_keyboard(channel_db_id)
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
        return

    if data.startswith(_CB_SET):
        parts = data[len(_CB_SET):].split(":")
        if len(parts) != 2:
            return
        try:
            channel_db_id = int(parts[0])
            schedule_id = int(parts[1])
        except ValueError:
            return

        schedule = await db.get_schedule_for_user(user_id, schedule_id)
        if schedule is None or int(schedule.get("channel_id", -1)) != channel_db_id:
            await query.answer("Schedule not found.", show_alert=True)
            return

        await db.set_user_context(
            user_id=user_id,
            selected_channel_id=channel_db_id,
            selected_schedule_id=schedule_id,
        )

        details = await db.get_user_context_details(user_id)
        channel_name = details.get("channel_name") or f"Channel {channel_db_id}"
        schedule_name = details.get("schedule_name") or f"Schedule {schedule_id}"
        try:
            await query.edit_message_text(f"Selected: {channel_name} / {schedule_name}.")
        except Exception:
            pass
        logger.info("User %s selected channel=%s schedule=%s", user_id, channel_db_id, schedule_id)
