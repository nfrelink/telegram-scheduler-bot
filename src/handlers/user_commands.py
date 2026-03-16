"""Basic user-facing commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db
from .common import ensure_user_record
from .timezone_management import send_timezone_prompt
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def _help_text() -> str:
    return (
        "Available commands:\n"
        "\n"
        "- /select — Pick active channel and schedule\n"
        "- /queue — Browse and manage the post queue\n"
        "- /timezone — Set your display timezone\n"
        "- /channels — Add or remove channels\n"
        "- /schedules — Create, edit, and manage schedules\n"
        "- /forward — Manage native-forwarding allowlist\n"
        "\n"
        "Bulk upload:\n"
        "- /bulk [schedule_id] — Start queuing posts\n"
        "- /done (inside bulk upload)\n"
        "- /cancel\n"
    )


def _onboarding_segments() -> list[Segment]:
    return [
        Segment("Quick start:\n"),
        Segment("1) Use "),
        Segment("/channels"),
        Segment(" to add a channel (the bot must be an admin there)\n"),
        Segment("2) Use "),
        Segment("/schedules"),
        Segment(" to create a posting schedule\n"),
        Segment("3) Use "),
        Segment("/select"),
        Segment(" to pick a channel and schedule as your active target\n"),
        Segment("4) Send media or use "),
        Segment("/bulk"),
        Segment(" to queue posts\n"),
        Segment("5) Use "),
        Segment("/queue"),
        Segment(" to browse and manage what is queued\n"),
    ]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with command overview."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    try:
        details = await db.get_user_context_details(user_id)

        segments: list[Segment] = [
            Segment("Telegram Scheduler Bot is running.\n\n"),
            *_onboarding_segments(),
            Segment("\nType "),
            Segment("/help"),
            Segment(" to see all commands.\n"),
        ]

        if details.get("telegram_channel_id") or details.get("selected_schedule_id"):
            from handlers.selection import selection_segments  # local import

            segments += [Segment("\n"), *selection_segments(details)]

        text, entities = render(segments)
        await update.message.reply_text(text, entities=entities)

        # If timezone is not yet configured, prompt immediately after welcome.
        if not await db.get_user_timezone(user_id):
            await send_timezone_prompt(
                chat_id=update.effective_chat.id,
                bot=context.bot,
            )

        logger.info("Handled /start for user_id=%s", user_id)
    except Exception as e:
        logger.error("Error in start_command for user_id=%s: %s", user_id, e, exc_info=True)
        await update.message.reply_text("An error occurred. Please try again.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help text."""
    await ensure_user_record(update, context)

    if update.message is None:
        return

    help_text = _help_text()
    details = await db.get_user_context_details(update.effective_user.id) if update.effective_user else {}

    if details.get("telegram_channel_id") or details.get("selected_schedule_id"):
        from handlers.selection import selection_segments  # local import

        segments = [Segment(help_text), Segment("\n\n"), *selection_segments(details)]
        text, entities = render(segments)
        await update.message.reply_text(text, entities=entities)
        return

    await update.message.reply_text(help_text)

