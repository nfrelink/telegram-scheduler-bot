"""Bot initialization and handler registration."""

from __future__ import annotations

import logging
import os

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from handlers.admin import broadcast_command, debug_command, stats_command
from handlers.channel_info import channelid_command
from handlers.channel_management import list_channels_command, remove_channel_command
from handlers.bulk_upload import bulk_upload_conversation_handler
from handlers.queue_management import delete_post_command, test_schedule_command, view_queue_command
from handlers.schedule_management import (
    copy_schedule_command,
    delete_schedule_command,
    edit_schedule_conversation_handler,
    list_schedules_command,
    new_schedule_conversation_handler,
    pause_schedule_command,
    resume_schedule_command,
)
from handlers.selection import (
    clearselection_command,
    selectchannel_command,
    selectschedule_command,
    selection_command,
)
from handlers.user_commands import help_command, start_command
from handlers.verification import add_channel_command, channel_post_handler

logger = logging.getLogger(__name__)

async def error_handler(update: object, context) -> None:  # type: ignore[no-untyped-def]
    """Global error handler."""
    logger.error("Unhandled exception while processing update=%r", update, exc_info=context.error)


def create_application() -> Application:
    """Create and configure the Telegram Application."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    application = Application.builder().token(token).build()

    # Core user commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # Channel verification and management
    application.add_handler(CommandHandler("addchannel", add_channel_command))
    application.add_handler(CommandHandler("channelid", channelid_command))
    application.add_handler(CommandHandler("listchannels", list_channels_command))
    application.add_handler(CommandHandler("removechannel", remove_channel_command))

    # Selection helpers
    application.add_handler(CommandHandler("selection", selection_command))
    application.add_handler(CommandHandler("selectchannel", selectchannel_command))
    application.add_handler(CommandHandler("selectschedule", selectschedule_command))
    application.add_handler(CommandHandler("clearselection", clearselection_command))

    # Bulk upload (Phase 4)
    application.add_handler(bulk_upload_conversation_handler)

    # Schedule management (Phase 3)
    application.add_handler(new_schedule_conversation_handler)
    application.add_handler(edit_schedule_conversation_handler)
    application.add_handler(CommandHandler("listschedules", list_schedules_command))
    application.add_handler(CommandHandler("pauseschedule", pause_schedule_command))
    application.add_handler(CommandHandler("resumeschedule", resume_schedule_command))
    application.add_handler(CommandHandler("deleteschedule", delete_schedule_command))
    application.add_handler(CommandHandler("copyschedule", copy_schedule_command))

    # Queue management (Phase 3)
    application.add_handler(CommandHandler("viewqueue", view_queue_command))
    application.add_handler(CommandHandler("deletepost", delete_post_command))
    application.add_handler(CommandHandler("testschedule", test_schedule_command))

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

