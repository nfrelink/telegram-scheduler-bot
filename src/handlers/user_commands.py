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
        "- /duplicates — Manage duplicate detection settings\n"
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


async def _pending_upload_nudge(user_id: int) -> str | None:
    """Return a hint if the user has an interrupted bulk upload."""
    session = await db.get_bulk_session(user_id)
    if session is None:
        return None
    count = await db.get_staging_count(user_id)
    if count <= 0:
        return None
    return f"You have {count} item(s) from an interrupted upload. Send /bulk to resume or discard them."


async def _onboarding_nudge(user_id: int) -> str | None:
    """Return a contextual next-step hint, or None if setup is complete."""
    channels = await db.get_user_channels(user_id)
    if not channels:
        return "Next step: Add your first channel with /channels"

    has_any_schedule = False
    for ch in channels:
        schedules = await db.get_channel_schedules(int(ch["id"]))
        if schedules:
            has_any_schedule = True
            break

    if not has_any_schedule:
        return "Next step: Create a posting schedule with /schedules"

    ctx = await db.get_user_context(user_id)
    if not ctx.get("selected_channel_id") or not ctx.get("selected_schedule_id"):
        return "Next step: Use /select to pick your active channel and schedule"

    return None


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

        nudge = await _onboarding_nudge(user_id)
        if nudge:
            await update.message.reply_text(nudge)

        upload_nudge = await _pending_upload_nudge(user_id)
        if upload_nudge:
            await update.message.reply_text(upload_nudge)

        logger.info("Handled /start for user_id=%s", user_id)
    except Exception as e:
        logger.error(
            "Error in start_command for user_id=%s: %s", user_id, e, exc_info=True
        )
        await update.message.reply_text("An error occurred. Please try again.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help text."""
    await ensure_user_record(update, context)

    if update.message is None:
        return

    help_text = _help_text()
    details = (
        await db.get_user_context_details(update.effective_user.id)
        if update.effective_user
        else {}
    )

    if details.get("telegram_channel_id") or details.get("selected_schedule_id"):
        from handlers.selection import selection_segments  # local import

        segments = [Segment(help_text), Segment("\n\n"), *selection_segments(details)]
        text, entities = render(segments)
        await update.message.reply_text(text, entities=entities)
    else:
        await update.message.reply_text(help_text)

    if update.effective_user:
        upload_nudge = await _pending_upload_nudge(update.effective_user.id)
        if upload_nudge:
            await update.message.reply_text(upload_nudge)


async def pending_media_nudge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Low-priority fallback: remind user about pending staging when they send
    media outside of any active conversation (e.g. after a bot restart)."""
    if update.effective_user is None or update.message is None:
        return
    if context.user_data.get("bulk_schedule_id") is not None:
        return
    session = await db.get_bulk_session(update.effective_user.id)
    if session is None:
        return
    count = await db.get_staging_count(update.effective_user.id)
    if count <= 0:
        return
    await update.message.reply_text(
        f"This media was not added — no active upload session.\n"
        f"You have {count} item(s) from a previous upload. Send /bulk to resume or discard them."
    )
