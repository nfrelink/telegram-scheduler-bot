"""Channel listing/removal commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db

from .common import ensure_user_record
from .selection import selection_segments
from .verification import resolve_channel_id
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List verified channels owned by the user."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    channels = await db.get_user_channels(user_id)

    if not channels:
        await update.message.reply_text(
            "You have no verified channels yet.\n\n"
            "To add one:\n"
            "1) Add this bot to your channel as an administrator (with permission to post messages)\n"
            "2) If you don't know the numeric channel id, post /channelid in the channel\n"
            "3) Then run: /addchannel <@channel or -100...>"
        )
        return

    segments: list[Segment] = [Segment("Your verified channels:\n")]
    for ch in channels:
        segments += [
            Segment("- "),
            Segment(str(ch["channel_name"])),
            Segment(" ("),
            Segment(str(ch["channel_id"]), code=True),
            Segment(")\n"),
        ]

    segments += [
        Segment("\nTip: set a default channel with /selectchannel "),
        Segment(str(channels[0]["channel_id"]), code=True),
        Segment(" (replace with the channel id you want).\n\n"),
    ]

    details = await db.get_user_context_details(user_id)
    segments += selection_segments(details)

    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a verified channel (cascades to schedules and queue)."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /removechannel <@channelname or -100...>\n"
            "Tip: use /listchannels to copy the channel id."
        )
        return

    raw = context.args[0]
    telegram_channel_id = await resolve_channel_id(context, raw)
    if telegram_channel_id is None:
        await update.message.reply_text(
            "Could not resolve that channel. Try using the numeric id shown in /listchannels."
        )
        return

    channel = await db.get_channel_by_telegram_id(telegram_channel_id)
    if channel is None or int(channel["user_id"]) != user_id:
        await update.message.reply_text(
            "Channel not found or you don't have permission to remove it."
        )
        return

    await db.delete_channel(int(channel["id"]))
    text, entities = render(
        [
            Segment("Removed channel '"),
            Segment(str(channel["channel_name"])),
            Segment("' ("),
            Segment(str(telegram_channel_id), code=True),
            Segment(")."),
        ]
    )
    await update.message.reply_text(text, entities=entities)
    logger.info("User %s removed channel %s", user_id, telegram_channel_id)

