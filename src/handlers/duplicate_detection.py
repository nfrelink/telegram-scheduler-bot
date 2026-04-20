"""Handler for the /duplicates command (per-channel and per-user toggle)."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, ConversationHandler, CommandHandler

from database import queries as db
from handlers.common import ensure_user_record
from services import dedup

logger = logging.getLogger(__name__)

VIEWING = 0


async def duplicates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /duplicates — show current status and toggle buttons."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    user_id = update.effective_user.id
    user_ctx = await db.get_user_context(user_id)
    channel_db_id = user_ctx.get("selected_channel_id")

    if channel_db_id is None:
        await update.message.reply_text(
            "No channel selected. Use /select to pick a channel first."
        )
        return ConversationHandler.END

    channel = await db.get_channel_by_id(int(channel_db_id))
    if channel is None:
        await update.message.reply_text("Selected channel not found. Use /select to pick one.")
        return ConversationHandler.END

    text, keyboard = await _build_status_message(user_id, int(channel_db_id), channel)
    await update.message.reply_text(text, reply_markup=keyboard)
    return VIEWING


async def duplicates_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle toggle button presses."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return VIEWING

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    user_ctx = await db.get_user_context(user_id)
    channel_db_id = user_ctx.get("selected_channel_id")
    if channel_db_id is None:
        try:
            await query.edit_message_text("No channel selected.")
        except Exception:
            pass
        return ConversationHandler.END

    channel_db_id = int(channel_db_id)
    channel = await db.get_channel_by_id(channel_db_id)
    if channel is None:
        try:
            await query.edit_message_text("Channel not found.")
        except Exception:
            pass
        return ConversationHandler.END

    if data == "dupset:toggle_channel":
        current = await dedup.is_channel_scanning_enabled(channel_db_id)
        await dedup.set_channel_scanning_enabled(channel_db_id, enabled=not current)
    elif data == "dupset:toggle_user":
        current = await dedup.is_user_alerts_enabled(user_id)
        await dedup.set_user_alerts_enabled(user_id, enabled=not current)
    text, keyboard = await _build_status_message(user_id, channel_db_id, channel)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        pass
    return VIEWING


async def _build_status_message(
    user_id: int, channel_db_id: int, channel: dict
) -> tuple[str, InlineKeyboardMarkup]:
    channel_enabled = await dedup.is_channel_scanning_enabled(channel_db_id)
    user_enabled = await dedup.is_user_alerts_enabled(user_id)

    channel_name = channel.get("channel_name") or channel.get("channel_id") or "channel"
    channel_status = "enabled" if channel_enabled else "disabled"
    user_status = "on" if user_enabled else "muted"

    text = (
        f"Duplicate detection for '{channel_name}':\n"
        f"  Channel scanning: {channel_status}\n"
        f"  Your alerts: {user_status}\n"
    )

    channel_btn_label = "Disable for channel" if channel_enabled else "Enable for channel"
    user_btn_label = "Mute my alerts" if user_enabled else "Unmute my alerts"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(channel_btn_label, callback_data="dupset:toggle_channel")],
        [InlineKeyboardButton(user_btn_label, callback_data="dupset:toggle_user")],
    ])
    return text, keyboard


duplicates_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("duplicates", duplicates_command)],
    allow_reentry=True,
    states={
        VIEWING: [
            CallbackQueryHandler(duplicates_callback, pattern=r"^dupset:"),
        ],
    },
    fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
)
