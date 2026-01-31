"""Helpers for extracting channel identifiers."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


async def channelid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the channel's Telegram ID for easier /addchannel usage."""
    chat = update.effective_chat
    if chat is None:
        return

    if chat.type != ChatType.CHANNEL:
        # In private/group chats, this isn't very helpful.
        if update.effective_message:
            await update.effective_message.reply_text(
                "Run /channelid inside the target channel (as a channel post) to get its numeric id."
            )
        return

    telegram_channel_id = str(chat.id)
    channel_name = chat.title or (f"@{chat.username}" if chat.username else telegram_channel_id)
    username = f"@{chat.username}" if getattr(chat, "username", None) else None

    segments: list[Segment] = [
        Segment("Channel info:\n"),
        Segment("- Name: "),
        Segment(channel_name),
        Segment("\n"),
        Segment("- Telegram ID: "),
        Segment(telegram_channel_id, code=True),
    ]
    if username:
        segments += [Segment("\n- Username: "), Segment(username, code=True)]

    segments += [
        Segment("\n\nTo verify this channel, send this command to me in private:\n"),
        Segment("/addchannel "),
        Segment(telegram_channel_id, code=True),
    ]

    text, entities = render(segments)
    await context.bot.send_message(chat_id=chat.id, text=text, entities=entities)

