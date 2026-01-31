"""Channel ownership verification flow."""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from database import queries as db

from .common import ensure_user_record
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


_CODE_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_-]{15,64}")


async def _get_bot_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = getattr(context.bot, "id", None)
    if bot_id:
        return int(bot_id)

    me = await context.bot.get_me()
    return int(me.id)


async def resolve_channel_id(context: ContextTypes.DEFAULT_TYPE, raw: str) -> str | None:
    """Resolve a user-supplied channel identifier to a Telegram channel id string.

    If resolution fails (e.g. bot removed from channel), returns None.
    """
    try:
        chat = await context.bot.get_chat(raw)
    except Exception:
        # If it's already a numeric id, let the caller proceed with that.
        if raw.lstrip("-").isdigit():
            return raw
        return None

    return str(chat.id)


async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start channel verification by generating a code."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /addchannel <channel>\n"
            "Example: /addchannel @mychannel\n"
            "Example: /addchannel -1001234567890\n\n"
            "Important: I must be added to the channel as an administrator (with permission to post messages).\n"
            "If you don't know the numeric channel id, add me as admin and post /channelid in the channel."
        )
        return

    raw_channel = context.args[0]

    try:
        chat = await context.bot.get_chat(raw_channel)
    except Exception as e:
        logger.warning("User %s: could not resolve channel %r: %s", user_id, raw_channel, e)
        await update.message.reply_text(
            "Could not access this channel. Please check:\n"
            "- The channel identifier is correct\n"
            "- I have been added to the channel as an administrator\n"
            "- I have permission to post messages\n\n"
            "Tip: if you don't know the numeric channel id, post /channelid in the channel after adding me as admin."
        )
        return

    if chat.type != ChatType.CHANNEL:
        await update.message.reply_text("That chat is not a channel. Please provide a channel.")
        return

    telegram_channel_id = str(chat.id)
    channel_name = chat.title or (f"@{chat.username}" if chat.username else telegram_channel_id)

    existing = await db.get_channel_by_telegram_id(telegram_channel_id)
    if existing is not None and int(existing["user_id"]) == user_id:
        text, entities = render(
            [
                Segment("Channel '"),
                Segment(str(existing["channel_name"])),
                Segment("' is already verified.\nTelegram ID: "),
                Segment(str(existing["channel_id"]), code=True),
            ]
        )
        await update.message.reply_text(text, entities=entities)
        return

    try:
        bot_id = await _get_bot_id(context)
        bot_member = await context.bot.get_chat_member(chat.id, bot_id)
        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                "I am not an admin in this channel.\n"
                "Please add me as an administrator with posting privileges first."
            )
            return

        # If admin, ensure we can post
        if bot_member.status == "administrator" and hasattr(bot_member, "can_post_messages"):
            if not getattr(bot_member, "can_post_messages", False):
                await update.message.reply_text(
                    "I am an admin, but I don't have permission to post messages.\n"
                    "Please grant me posting privileges."
                )
                return

        user_member = await context.bot.get_chat_member(chat.id, user_id)
        if user_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                "You are not an admin of this channel.\n"
                "Only channel admins can add channels to the bot."
            )
            return

    except Exception as e:
        logger.error("User %s: failed admin checks for channel %s: %s", user_id, telegram_channel_id, e, exc_info=True)
        await update.message.reply_text(
            "I couldn't verify permissions for this channel.\n"
            "Make sure you added me as an administrator and try again."
        )
        return

    code = await db.create_verification_code(user_id=user_id, telegram_channel_id=telegram_channel_id)

    await update.message.reply_text(
        "Step 1 complete: permissions verified.\n\n"
        "Step 2: Post this exact code to the channel to verify ownership:\n\n"
        f"{code}\n\n"
        "I will detect it automatically. The code expires in 10 minutes."
    )
    logger.info("User %s: issued verification code for channel %s", user_id, telegram_channel_id)


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
            Segment("\n\nYou can now manage it with:\n"),
            Segment("/listchannels\n"),
            Segment("/removechannel "),
            Segment(telegram_channel_id, code=True),
            Segment("\n"),
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

