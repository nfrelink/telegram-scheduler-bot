"""Bot initialization and handler registration."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import time

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from handlers.admin import broadcast_command, debug_command, stats_command
from handlers.channel_management import channels_conversation_handler
from handlers.bulk_upload import bulk_upload_conversation_handler
from handlers.duplicate_detection import duplicates_conversation_handler
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
from handlers.user_commands import help_command, pending_media_nudge, start_command
from handlers.verification import channel_post_handler

logger = logging.getLogger(__name__)
_TELEGRAM_TEXT_LIMIT = 4096
_DEFAULT_ADMIN_ERROR_DM_DEBOUNCE_SECONDS = 60
_ADMIN_ERROR_DM_LAST_SENT: dict[str, float] = {}

USER_COMMANDS: list[BotCommand] = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show available commands"),
    BotCommand("select", "Pick active channel and schedule"),
    BotCommand("queue", "Browse and manage the post queue"),
    BotCommand("timezone", "Set your display timezone"),
    BotCommand("channels", "Add or remove channels"),
    BotCommand("schedules", "Create, edit, and manage schedules"),
    BotCommand("forward", "Manage native-forwarding allowlist"),
    BotCommand("bulk", "Start queuing posts for bulk upload"),
    BotCommand("duplicates", "Manage duplicate detection settings"),
    BotCommand("cancel", "Cancel current operation"),
]


async def register_commands(application: Application) -> None:
    """Register the bot's command menu with Telegram."""
    try:
        await application.bot.set_my_commands(USER_COMMANDS)
        logger.info("Registered %d bot commands with Telegram", len(USER_COMMANDS))
    except Exception:
        logger.exception("Failed to register bot commands")


def _get_admin_user_id() -> int | None:
    raw = os.getenv("ADMIN_USER_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not an integer")
        return None


def _get_admin_error_dm_debounce_seconds() -> int:
    raw = os.getenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS")
    if not raw:
        return _DEFAULT_ADMIN_ERROR_DM_DEBOUNCE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("ADMIN_ERROR_DM_DEBOUNCE_SECONDS is not an integer")
        return _DEFAULT_ADMIN_ERROR_DM_DEBOUNCE_SECONDS


def _error_signature(err: Exception | None) -> str:
    error_type = type(err).__name__ if err is not None else "UnknownError"
    error_message = str(err).strip() if err is not None else ""
    return f"{error_type}:{error_message}"


def _should_send_admin_error_dm(err: Exception | None) -> bool:
    debounce_seconds = _get_admin_error_dm_debounce_seconds()
    if debounce_seconds <= 0:
        return True

    signature = _error_signature(err)
    now = time.monotonic()
    last_sent_at = _ADMIN_ERROR_DM_LAST_SENT.get(signature)
    if last_sent_at is not None and (now - last_sent_at) < debounce_seconds:
        return False

    _ADMIN_ERROR_DM_LAST_SENT[signature] = now
    return True


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


def _format_admin_error_notification(meta: dict[str, object], err: Exception | None) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    error_type = type(err).__name__ if err is not None else "UnknownError"
    error_message = str(err).strip() if err is not None else ""
    if not error_message:
        error_message = "(no error message)"

    msg = (
        f"Unhandled bot error ({now_utc} UTC)\n"
        f"- type: {error_type}\n"
        f"- message: {error_message}\n"
        f"- update_id: {meta.get('update_id')}\n"
        f"- user_id: {meta.get('user_id')}\n"
        f"- chat_id: {meta.get('chat_id')}\n"
        f"- message_id: {meta.get('message_id')}"
    )
    if len(msg) <= _TELEGRAM_TEXT_LIMIT:
        return msg
    return msg[: _TELEGRAM_TEXT_LIMIT - 3] + "..."


async def error_handler(update: object, context) -> None:  # type: ignore[no-untyped-def]
    """Global error handler."""
    meta = _safe_update_meta(update)
    logger.error(
        "Unhandled exception while processing update (meta=%s)",
        meta,
        exc_info=context.error,
    )
    admin_user_id = _get_admin_user_id()
    if admin_user_id is None:
        return

    if not _should_send_admin_error_dm(context.error):
        logger.debug("Suppressed duplicate admin error DM (debounce active)")
        return

    try:
        text = _format_admin_error_notification(meta, context.error)
        await context.bot.send_message(chat_id=admin_user_id, text=text)
    except Exception:
        logger.exception("Failed to send admin error notification")


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

    # Duplicate detection settings (/duplicates)
    application.add_handler(duplicates_conversation_handler)

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

    # Low-priority fallback: nudge users with pending staging data when they
    # send media outside of any active conversation (e.g. after a bot restart).
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
            pending_media_nudge,
        ),
        group=99,
    )

    application.add_error_handler(error_handler)
    return application

