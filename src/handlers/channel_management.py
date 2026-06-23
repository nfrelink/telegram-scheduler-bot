"""Channel management — single /channels command.

/channels shows the user's verified channels with inline Remove buttons and an
Add channel button.  Remove shows cascade counts and asks for confirmation.
Add puts the conversation into AWAITING_ADD where the user types a channel ID or
@handle; the bot does admin checks and issues a verification code.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import queries as db
from handlers.common import ensure_user_record, safe_edit_message_text
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

_SHOWING = 0
_AWAITING_ADD = 1


async def _channels_list_text_and_keyboard(
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the channel list display."""
    channels = await db.get_user_channels(user_id)
    if not channels:
        text = (
            "You have no verified channels.\n\n"
            "To add one:\n"
            "1) Add this bot to the channel as an administrator (posting permission required)\n"
            "2) Tap Add channel below, then either send the channel's numeric ID or @handle, "
            "or simply forward any message from that channel here"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Add channel", callback_data="ch:add")],
            ]
        )
    else:
        n = len(channels)
        text = f"Your verified channels ({n}):"
        rows: list[list[InlineKeyboardButton]] = []
        for ch in channels:
            ch_id = int(ch["id"])
            name = str(ch.get("channel_name") or ch.get("channel_id") or f"Channel {ch_id}")
            count = await db.get_channel_queue_count(ch_id)
            label = f"{name}  ({count} queued)" if count else name
            rows.append(
                [
                    InlineKeyboardButton(label, callback_data="ch:noop"),
                    InlineKeyboardButton("Remove", callback_data=f"ch:rm:{ch_id}"),
                ]
            )
        rows.append([InlineKeyboardButton("Add channel", callback_data="ch:add")])
        keyboard = InlineKeyboardMarkup(rows)
    return text, keyboard


async def _get_bot_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = getattr(context.bot, "id", None)
    if bot_id:
        return int(bot_id)
    me = await context.bot.get_me()
    return int(me.id)


async def _verify_channel_admin_access(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    telegram_channel_id: str,
    reply_fn: Callable[..., Awaitable[None]],
) -> bool:
    """Return True when bot and user have admin access to post."""
    try:
        bot_id = await _get_bot_id(context)
        bot_member = await context.bot.get_chat_member(chat_id, bot_id)
        if bot_member.status not in ("administrator", "creator"):
            await reply_fn(
                "I am not an admin in that channel.\n"
                "Add me as an administrator with posting permission first."
            )
            return False
        if bot_member.status == "administrator" and not getattr(
            bot_member, "can_post_messages", True
        ):
            await reply_fn(
                "I am an admin but do not have permission to post messages.\n"
                "Please grant me posting permission."
            )
            return False
        user_member = await context.bot.get_chat_member(chat_id, user_id)
        if user_member.status not in ("administrator", "creator"):
            await reply_fn("You are not an admin of that channel.")
            return False
    except Exception:
        logger.exception(
            "Admin check failed for channel %s user %s",
            telegram_channel_id,
            user_id,
        )
        await reply_fn(
            "Could not verify permissions. Make sure you added me as administrator and try again."
        )
        return False
    return True


async def _run_add_flow(
    raw: str,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reply_fn: Callable[..., Awaitable[None]],
) -> None:
    """Resolve, admin-check, and issue a verification code for a channel.

    reply_fn is an async callable with signature (text: str, **kwargs) used to
    send responses back to the user.
    """
    try:
        chat = await context.bot.get_chat(raw)
    except Exception as e:
        logger.warning("Could not resolve channel %r for user %s: %s", raw, user_id, e)
        await reply_fn(
            "Could not access that channel. Check that:\n"
            "- The ID or @handle is correct\n"
            "- I have been added as an administrator with posting permission"
        )
        return

    if chat.type != ChatType.CHANNEL:
        await reply_fn("That is not a channel. Please send a channel ID or @handle.")
        return

    telegram_channel_id = str(chat.id)
    channel_name = chat.title or (f"@{chat.username}" if chat.username else telegram_channel_id)

    existing = await db.get_channel_by_telegram_id(telegram_channel_id)
    if existing is not None and int(existing["user_id"]) == user_id:
        text, entities = render(
            [
                Segment("Channel '"),
                Segment(str(existing["channel_name"])),
                Segment("' is already verified (ID: "),
                Segment(telegram_channel_id, code=True),
                Segment(")."),
            ]
        )
        await reply_fn(text, entities=entities)
        return

    if not await _verify_channel_admin_access(
        context, chat.id, user_id, telegram_channel_id, reply_fn
    ):
        return

    code = await db.create_verification_code(
        user_id=user_id, telegram_channel_id=telegram_channel_id
    )
    await reply_fn(
        f"Permissions verified for '{channel_name}'.\n\n"
        "Now post this code to the channel to complete verification:\n\n"
        f"{code}\n\n"
        "The bot will detect it automatically. The code expires in 10 minutes."
    )
    logger.info("User %s: issued verification code for channel %s", user_id, telegram_channel_id)


def _ch_parse_id(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    try:
        return int(data[len(prefix) :])
    except ValueError:
        return None


async def _ch_add(_query: CallbackQuery, _user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_edit_message_text(
        _query,
        "Send the channel ID (e.g. -100123456789) or @handle, "
        "or forward any message from the channel here.\n\n"
        "/cancel to abort.",
    )
    return _AWAITING_ADD


async def _ch_rm(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    ch_id = _ch_parse_id(query.data or "", "ch:rm:")
    if ch_id is None:
        return _SHOWING

    channels = await db.get_user_channels(user_id)
    owned = {int(c["id"]): c for c in channels}
    if ch_id not in owned:
        await query.answer("Channel not found.", show_alert=True)
        return _SHOWING

    ch = owned[ch_id]
    name = str(ch.get("channel_name") or f"Channel {ch_id}")
    schedules = await db.get_channel_schedules(ch_id)
    n_sched = len(schedules)
    n_posts = await db.get_channel_queue_count(ch_id)

    lines = [f"Remove '{name}'?"]
    if n_sched or n_posts:
        lines.append(f"This will also delete {n_sched} schedule(s) and {n_posts} queued post(s).")
    await safe_edit_message_text(
        query,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, remove", callback_data=f"ch:rmok:{ch_id}"),
                    InlineKeyboardButton("Cancel", callback_data="ch:back"),
                ]
            ]
        ),
    )
    return _SHOWING


async def _ch_rmok(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    ch_id = _ch_parse_id(query.data or "", "ch:rmok:")
    if ch_id is None:
        return _SHOWING

    channel = await db.get_channel_by_id_for_user(user_id, ch_id)
    if channel is None:
        await query.answer("Channel not found.", show_alert=True)
        return _SHOWING

    ch_name = str(channel.get("channel_name") or f"Channel {ch_id}")
    await db.delete_channel(ch_id, user_id=user_id)
    logger.info("User %s removed channel db_id=%s (%s)", user_id, ch_id, ch_name)

    text, keyboard = await _channels_list_text_and_keyboard(user_id)
    await safe_edit_message_text(query, text, reply_markup=keyboard)
    return ConversationHandler.END


async def _ch_back(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    text, keyboard = await _channels_list_text_and_keyboard(user_id)
    await safe_edit_message_text(query, text, reply_markup=keyboard)
    return _SHOWING


async def _ch_noop(
    _query: CallbackQuery, _user_id: int, _context: ContextTypes.DEFAULT_TYPE
) -> int:
    return _SHOWING


_CH_EXACT_HANDLERS: dict[str, object] = {
    "ch:add": _ch_add,
    "ch:back": _ch_back,
    "ch:noop": _ch_noop,
}

_CH_PREFIX_HANDLERS: tuple[tuple[str, object], ...] = (
    ("ch:rm:", _ch_rm),
    ("ch:rmok:", _ch_rmok),
)


async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/channels — show the channel list with action buttons."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None or update.effective_chat is None:
        return ConversationHandler.END

    text, keyboard = await _channels_list_text_and_keyboard(update.effective_user.id)
    sent = await update.message.reply_text(text, reply_markup=keyboard)
    context.user_data["ch_msg_id"] = sent.message_id
    context.user_data["ch_chat_id"] = update.effective_chat.id
    return _SHOWING


async def channels_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle ch:* inline keyboard callbacks."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return _SHOWING

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    exact = _CH_EXACT_HANDLERS.get(data)
    if exact is not None:
        return await exact(query, user_id, _context)  # type: ignore[operator]

    for prefix, handler in _CH_PREFIX_HANDLERS:
        if data.startswith(prefix):
            return await handler(query, user_id, _context)  # type: ignore[operator]

    return _SHOWING


async def channels_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user's text channel ID, @handle, or forwarded message during AWAITING_ADD."""
    msg = update.message
    if msg is None or update.effective_user is None:
        return _AWAITING_ADD

    user_id = update.effective_user.id

    # Accept a forwarded message from a channel — extract the channel ID from it.
    raw: str | None = None
    fwd_chat = getattr(msg, "forward_from_chat", None)
    if fwd_chat is not None:
        raw = str(fwd_chat.id)
    else:
        origin = getattr(msg, "forward_origin", None)
        if origin is not None and getattr(origin, "chat", None) is not None:
            raw = str(origin.chat.id)

    # Fall back to typed text (numeric ID or @handle).
    if raw is None:
        raw = (msg.text or "").strip() or None

    if not raw:
        await msg.reply_text(
            "Please send the channel ID (e.g. -100123456789), @handle, "
            "or forward any message from the channel here."
        )
        return _AWAITING_ADD

    await _run_add_flow(raw, user_id, context, msg.reply_text)
    return ConversationHandler.END


async def channels_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the /channels conversation."""
    if update.message is not None:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


channels_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("channels", channels_command)],
    allow_reentry=True,
    states={
        _SHOWING: [CallbackQueryHandler(channels_callback, pattern=r"^ch:")],
        _AWAITING_ADD: [
            MessageHandler(~filters.COMMAND, channels_add_handler),
        ],
    },
    fallbacks=[CommandHandler("cancel", channels_cancel)],
    name="channels",
    persistent=True,
)
