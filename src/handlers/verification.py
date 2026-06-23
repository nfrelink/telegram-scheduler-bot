"""Channel ownership verification flow."""

from __future__ import annotations

import logging
import re

from telegram import Message, Update
from telegram.ext import ContextTypes

from database import queries as db
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

_CODE_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_-]{15,64}")
_MAX_CODE_CANDIDATES = 10


async def _match_verification_code(
    text: str, telegram_channel_id: str
) -> tuple[int | None, str | None]:
    candidates = list(dict.fromkeys(_CODE_CANDIDATE_RE.findall(text)))
    if not candidates:
        return None, None

    for candidate in candidates[:_MAX_CODE_CANDIDATES]:
        user_id = await db.verify_code(code=candidate, telegram_channel_id=telegram_channel_id)
        if user_id is not None:
            return int(user_id), candidate
    return None, None


async def _resolve_channel_db_id(
    *,
    telegram_channel_id: str,
    channel_name: str,
    matched_user_id: int,
    bot,
) -> int | None:
    existing = await db.get_channel_by_telegram_id(telegram_channel_id)
    if existing is None:
        channel = await db.create_channel(
            user_id=matched_user_id,
            telegram_channel_id=telegram_channel_id,
            channel_name=channel_name,
        )
        return int(channel["id"])

    if int(existing["user_id"]) != matched_user_id:
        logger.warning(
            "Verification code accepted for channel %s but channel already belongs "
            "to user %s (attempt by %s)",
            telegram_channel_id,
            existing["user_id"],
            matched_user_id,
        )
        await bot.send_message(
            chat_id=matched_user_id,
            text=(
                f"Verification detected in '{channel_name}', but this channel is "
                "already registered.\n"
                "If you believe this is wrong, contact the bot administrator."
            ),
        )
        return None

    channel_db_id = int(existing["id"])
    if existing.get("channel_name") != channel_name:
        await db.update_channel_name(
            channel_db_id, channel_name=channel_name, user_id=matched_user_id
        )
    return channel_db_id


async def _delete_verification_message(message: Message, telegram_channel_id: str) -> str:
    try:
        await message.delete()
    except Exception as e:
        logger.info(
            "Could not delete verification message in channel %s: %s",
            telegram_channel_id,
            e,
        )
        return "Please delete the verification message from the channel manually."
    else:
        return "The verification message has been deleted."


async def _notify_verification_success(
    *,
    bot,
    matched_user_id: int,
    channel_name: str,
    channel_db_id: int,
    deletion_msg: str,
) -> None:
    schedules = await db.get_channel_schedules(channel_db_id)
    if schedules:
        next_step = "Use /channels to manage your channels."
    else:
        next_step = "Next step: Create a posting schedule with /schedules"

    msg_text, msg_entities = render(
        [
            Segment("Channel '"),
            Segment(channel_name),
            Segment("' has been successfully verified.\n\n"),
            Segment(deletion_msg),
            Segment("\n\n"),
            Segment(next_step),
        ]
    )
    await bot.send_message(
        chat_id=matched_user_id,
        text=msg_text,
        entities=msg_entities,
    )


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect posted verification codes in channels and complete verification."""
    message = update.channel_post
    if message is None:
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return

    telegram_channel_id = str(message.chat.id)
    matched_user_id, matched_code = await _match_verification_code(text, telegram_channel_id)
    if matched_user_id is None:
        return

    channel_name = message.chat.title or (
        f"@{message.chat.username}" if message.chat.username else telegram_channel_id
    )

    channel_db_id = await _resolve_channel_db_id(
        telegram_channel_id=telegram_channel_id,
        channel_name=channel_name,
        matched_user_id=matched_user_id,
        bot=context.bot,
    )
    if channel_db_id is None:
        return

    await db.set_user_context(
        user_id=matched_user_id,
        selected_channel_id=channel_db_id,
        selected_schedule_id=None,
    )

    deletion_msg = await _delete_verification_message(message, telegram_channel_id)
    await _notify_verification_success(
        bot=context.bot,
        matched_user_id=matched_user_id,
        channel_name=channel_name,
        channel_db_id=channel_db_id,
        deletion_msg=deletion_msg,
    )

    logger.info(
        "Verified channel %s for user %s (code=%s)",
        telegram_channel_id,
        matched_user_id,
        "<matched>" if matched_code else "<none>",
    )
