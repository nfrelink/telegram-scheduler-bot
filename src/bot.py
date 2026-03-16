"""Bot initialization and handler registration."""

from __future__ import annotations

import logging
import os

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from handlers.admin import broadcast_command, debug_command, stats_command
from handlers.channel_management import channels_conversation_handler
from handlers.bulk_upload import bulk_upload_conversation_handler
from handlers.forwarding import forward_conversation_handler
from handlers.queue_management import (
    delete_post_command,
    pin_date_conversation_handler,
    queue_browser_callback,
    view_queue_command,
)
from handlers.schedule_management import schedules_conversation_handler
from handlers.selection import select_callback, select_command
from handlers.timezone_management import gettimezone_command, settimezone_command, timezone_callback, timezone_command
from handlers.user_commands import help_command, start_command
from handlers.verification import channel_post_handler

logger = logging.getLogger(__name__)


def _safe_update_meta(update: object) -> dict[str, object]:
    """Extract minimal, non-content metadata for logging."""
    meta: dict[str, object] = {"type": type(update).__name__}
    try:
        meta["update_id"] = getattr(update, "update_id", None)
        effective_user = getattr(update, "effective_user", None)
        effective_chat = getattr(update, "effective_chat", None)
        effective_message = getattr(update, "effective_message", None)
        meta["user_id"] = getattr(effective_user, "id", None) if effective_user is not None else None
        meta["chat_id"] = getattr(effective_chat, "id", None) if effective_chat is not None else None
        meta["message_id"] = (
            getattr(effective_message, "message_id", None) if effective_message is not None else None
        )
    except Exception:
        # Best-effort only; never raise from error handler logging.
        return meta
    return meta


async def error_handler(update: object, context) -> None:  # type: ignore[no-untyped-def]
    """Global error handler."""
    meta = _safe_update_meta(update)
    logger.error(
        "Unhandled exception while processing update (meta=%s)",
        meta,
        exc_info=context.error,
    )


def create_application() -> Application:
    """Create and configure the Telegram Application."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    application = Application.builder().token(token).build()

    # Core user commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("select", select_command))
    application.add_handler(CallbackQueryHandler(select_callback, pattern=r"^sc:"))
    application.add_handler(CommandHandler("timezone", timezone_command))
    application.add_handler(CommandHandler("gettimezone", gettimezone_command))
    application.add_handler(CommandHandler("settimezone", settimezone_command))
    application.add_handler(CallbackQueryHandler(timezone_callback, pattern=r"^tz:"))

    # Channel management (/channels)
    application.add_handler(channels_conversation_handler)

    # Forwarding allowlist (/forward)
    application.add_handler(forward_conversation_handler)

    # Bulk upload (Phase 4)
    application.add_handler(bulk_upload_conversation_handler)

    # Schedule management (/schedules)
    application.add_handler(schedules_conversation_handler)

    # Queue management (/queue alias + legacy /viewqueue, /deletepost)
    application.add_handler(CommandHandler("queue", view_queue_command))
    application.add_handler(CommandHandler("viewqueue", view_queue_command))
    application.add_handler(CommandHandler("deletepost", delete_post_command))
    # Pin-date conversation must be registered before the general qv: handler
    # so its qv:pd:* entry point takes priority.
    application.add_handler(pin_date_conversation_handler)
    application.add_handler(CallbackQueryHandler(queue_browser_callback, pattern=r"^qv:"))

    # Channel posts: verification code detection
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), channel_post_handler)
    )

    # Admin commands (restricted to ADMIN_USER_ID)
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))

    application.add_error_handler(error_handler)
    return application

