"""Channel ownership verification flow."""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db

from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


_CODE_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_-]{15,64}")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect posted verification codes in channels and complete verification."""
    message = update.channel_post
    if message is None:
        # Filters should prevent this, but keep it safe.
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return

    telegram_channel_id = str(message.chat.id)
    candidates = list(dict.fromkeys(_CODE_CANDIDATE_RE.findall(text)))
    if not candidates:
        return

    matched_user_id: int | None = None
    matched_code: str | None = None
    for candidate in candidates[:10]:
        user_id = await db.verify_code(code=candidate, telegram_channel_id=telegram_channel_id)
        if user_id is not None:
            matched_user_id = int(user_id)
            matched_code = candidate
            break

    if matched_user_id is None:
        return

    channel_name = message.chat.title or (f"@{message.chat.username}" if message.chat.username else telegram_channel_id)

    existing = await db.get_channel_by_telegram_id(telegram_channel_id)
    if existing is None:
        await db.create_channel(
            user_id=matched_user_id,
            telegram_channel_id=telegram_channel_id,
            channel_name=channel_name,
        )
    else:
        # If channel is already registered to someone else, do not reassign it.
        if int(existing["user_id"]) != matched_user_id:
            logger.warning(
                "Verification code accepted for channel %s but channel already belongs to user %s (attempt by %s)",
                telegram_channel_id,
                existing["user_id"],
                matched_user_id,
            )
            await context.bot.send_message(
                chat_id=matched_user_id,
                text=(
                    f"Verification detected in '{channel_name}', but this channel is already registered.\n"
                    "If you believe this is wrong, contact the bot administrator."
                ),
            )
            return

        # Keep channel_name fresh if it changed.
        if existing.get("channel_name") != channel_name:
            await db.update_channel_name(int(existing["id"]), channel_name=channel_name)

    # Try to delete the verification message.
    deletion_msg = ""
    try:
        await message.delete()
        deletion_msg = "The verification message has been deleted."
    except Exception as e:
        logger.info("Could not delete verification message in channel %s: %s", telegram_channel_id, e)
        deletion_msg = "Please delete the verification message from the channel manually."

    msg_text, msg_entities = render(
        [
            Segment("Channel '"),
            Segment(channel_name),
            Segment("' has been successfully verified.\n\n"),
            Segment(deletion_msg),
            Segment("\n\nUse /channels to manage your channels."),
        ]
    )
    await context.bot.send_message(
        chat_id=matched_user_id,
        text=msg_text,
        entities=msg_entities,
    )

    logger.info(
        "Verified channel %s for user %s (code=%s)",
        telegram_channel_id,
        matched_user_id,
        "<matched>" if matched_code else "<none>",
    )

