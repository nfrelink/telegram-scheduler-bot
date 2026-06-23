"""Forwarding allowlist management — single /forward command.

/forward shows the current allowlist with inline buttons to add, remove, or
clear entries.  Adding an entry puts the conversation into AWAITING_ADD, where
the user can type a numeric channel ID or forward any message from that channel.
"""

from __future__ import annotations

import contextlib
import logging

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

logger = logging.getLogger(__name__)

_SHOWING = 0
_AWAITING_ADD = 1


async def _list_text_and_keyboard(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the allowlist display text and inline keyboard."""
    origins = await db.get_forward_origin_allowlist_with_names(user_id)
    if not origins:
        text = (
            "Forwarding allowlist is empty.\n\n"
            "During /bulk, messages forwarded from allowlisted channels are sent "
            "as native Telegram forwards, preserving 'Forwarded from ...' attribution."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Add channel", callback_data="fw:add")],
            ]
        )
    else:
        n = len(origins)
        text = f"Forwarding allowlist — {n} channel{'s' if n > 1 else ''}:"
        rows: list[list[InlineKeyboardButton]] = []
        for cid, name in origins:
            label = name or str(cid)
            rows.append(
                [
                    InlineKeyboardButton(label, callback_data="fw:noop"),
                    InlineKeyboardButton("Remove", callback_data=f"fw:rm:{cid}"),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("Add channel", callback_data="fw:add"),
                InlineKeyboardButton("Clear all", callback_data="fw:clear"),
            ]
        )
        keyboard = InlineKeyboardMarkup(rows)
    return text, keyboard


def _fw_parse_id(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    try:
        return int(data[len(prefix) :])
    except ValueError:
        return None


async def _fw_add(_query: CallbackQuery, _user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_edit_message_text(
        _query,
        "Send the channel ID (e.g. -100123456789), or forward any message "
        "from that channel.\n\n/cancel to abort.",
    )
    return _AWAITING_ADD


async def _fw_rm(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    cid = _fw_parse_id(query.data or "", "fw:rm:")
    if cid is None:
        return _SHOWING
    await db.remove_forward_origin_allowlist(user_id=user_id, origin_chat_id=cid)
    text, keyboard = await _list_text_and_keyboard(user_id)
    await safe_edit_message_text(query, text, reply_markup=keyboard)
    return ConversationHandler.END


async def _fw_clear(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    origins = await db.get_forward_origin_allowlist(user_id)
    n = len(origins)
    label = "entry" if n == 1 else "entries"
    await safe_edit_message_text(
        query,
        f"Remove all {n} {label} from the forwarding allowlist?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, clear all", callback_data="fw:clearok"),
                    InlineKeyboardButton("Cancel", callback_data="fw:back"),
                ]
            ]
        ),
    )
    return _SHOWING


async def _fw_clearok(
    query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE
) -> int:
    await db.clear_forward_origin_allowlist(user_id)
    text, keyboard = await _list_text_and_keyboard(user_id)
    await safe_edit_message_text(query, text, reply_markup=keyboard)
    return ConversationHandler.END


async def _fw_back(query: CallbackQuery, user_id: int, _context: ContextTypes.DEFAULT_TYPE) -> int:
    text, keyboard = await _list_text_and_keyboard(user_id)
    await safe_edit_message_text(query, text, reply_markup=keyboard)
    return _SHOWING


async def _fw_noop(
    _query: CallbackQuery, _user_id: int, _context: ContextTypes.DEFAULT_TYPE
) -> int:
    return _SHOWING


_FW_EXACT_HANDLERS: dict[str, object] = {
    "fw:add": _fw_add,
    "fw:clear": _fw_clear,
    "fw:clearok": _fw_clearok,
    "fw:back": _fw_back,
    "fw:noop": _fw_noop,
}

_FW_PREFIX_HANDLERS: tuple[tuple[str, object], ...] = (("fw:rm:", _fw_rm),)


async def forward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/forward — show the allowlist with action buttons."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None or update.effective_chat is None:
        return ConversationHandler.END

    text, keyboard = await _list_text_and_keyboard(update.effective_user.id)
    sent = await update.message.reply_text(text, reply_markup=keyboard)
    context.user_data["fw_msg_id"] = sent.message_id
    context.user_data["fw_chat_id"] = update.effective_chat.id
    return _SHOWING


async def forward_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle fw:* inline keyboard callbacks."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return _SHOWING

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    exact = _FW_EXACT_HANDLERS.get(data)
    if exact is not None:
        return await exact(query, user_id, _context)  # type: ignore[operator]

    for prefix, handler in _FW_PREFIX_HANDLERS:
        if data.startswith(prefix):
            return await handler(query, user_id, _context)  # type: ignore[operator]

    return _SHOWING


async def _forward_detect_channel(
    msg, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int | None, str | None]:
    """Extract channel id and optional title from a forwarded or typed message."""
    channel_id: int | None = None
    channel_name: str | None = None

    if getattr(msg, "forward_from_chat", None) is not None:
        channel_id = msg.forward_from_chat.id
        channel_name = getattr(msg.forward_from_chat, "title", None) or None
    else:
        origin = getattr(msg, "forward_origin", None)
        if origin is not None and getattr(origin, "chat", None) is not None:
            channel_id = origin.chat.id
            channel_name = getattr(origin.chat, "title", None) or None

    if channel_id is None and msg.text:
        with contextlib.suppress(ValueError):
            channel_id = int(msg.text.strip())
        if channel_id is not None:
            with contextlib.suppress(Exception):
                chat = await context.bot.get_chat(channel_id)
                channel_name = getattr(chat, "title", None) or None

    return channel_id, channel_name


async def _forward_refresh_list_message(
    context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: InlineKeyboardMarkup
) -> bool:
    """Try to edit the stored /forward list message. Returns True on success."""
    fw_msg_id = context.user_data.get("fw_msg_id")
    fw_chat_id = context.user_data.get("fw_chat_id")
    if not fw_msg_id or not fw_chat_id:
        return False
    with contextlib.suppress(Exception):
        await context.bot.edit_message_text(
            chat_id=fw_chat_id,
            message_id=fw_msg_id,
            text=text,
            reply_markup=keyboard,
        )
        return True
    return False


async def forward_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user's text or forwarded message during AWAITING_ADD."""
    msg = update.message
    if msg is None or update.effective_user is None:
        return _AWAITING_ADD

    user_id = update.effective_user.id
    channel_id, channel_name = await _forward_detect_channel(msg, context)

    if channel_id is None:
        await msg.reply_text(
            "That doesn't look like a channel ID. "
            "Send a numeric channel ID (e.g. -100123456789) or forward a message from the channel."
        )
        return _AWAITING_ADD

    await db.add_forward_origin_allowlist(
        user_id=user_id, origin_chat_id=channel_id, origin_channel_name=channel_name
    )
    logger.info("User %s added forward origin %s (%s)", user_id, channel_id, channel_name)

    text, keyboard = await _list_text_and_keyboard(user_id)
    if await _forward_refresh_list_message(context, text, keyboard):
        return ConversationHandler.END
    await msg.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def forward_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the /forward conversation."""
    if update.message is not None:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


forward_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("forward", forward_command)],
    allow_reentry=True,
    states={
        _SHOWING: [CallbackQueryHandler(forward_callback, pattern=r"^fw:")],
        _AWAITING_ADD: [
            MessageHandler(~filters.COMMAND, forward_add_handler),
        ],
    },
    fallbacks=[CommandHandler("cancel", forward_cancel)],
    name="forward",
    persistent=True,
)
