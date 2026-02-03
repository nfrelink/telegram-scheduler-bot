"""Basic user-facing commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db
from .common import ensure_user_record
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def _help_text() -> str:
    return (
        "Available commands:\n"
        "\n"
        "- /start — Welcome message\n"
        "- /help — Show this help\n"
        "\n"
        "Timezone:\n"
        "- /gettimezone — Show your default timezone\n"
        "- /settimezone <timezone> — Set your default timezone (IANA name) for new schedules\n"
        "\n"
        "Channels:\n"
        "- /addchannel <@channel or -100...> — Verify a channel\n"
        "- /channelid — Post the channel ID (run inside the channel)\n"
        "- /listchannels — List your verified channels\n"
        "- /removechannel <@channel or -100...> — Remove a verified channel\n"
        "\n"
        "Selection (optional, but recommended):\n"
        "- /selectchannel <channel_id> — Set default channel\n"
        "- /selectschedule <schedule_id> — Set default schedule\n"
        "- /selection — Show current selection\n"
        "- /clearselection — Clear selection\n"
        "\n"
        "Forwarding (optional):\n"
        "- /forwarding — Show forwarding allowlist (origin channels)\n"
        "- /addforward <origin_channel_id> — Add origin channel to allowlist\n"
        "- /removeforward <origin_channel_id> — Remove origin channel from allowlist\n"
        "- /clearforward — Clear forwarding allowlist\n"
        "\n"
        "Note: forwarding only applies in /bulk when caption mode is 'preserve'.\n"
        "\n"
        "Tip: when a channel/schedule is selected, many commands work without an explicit id.\n"
        "\n"
        "Schedules:\n"
        "- /newschedule [channel_id] — Create a schedule (interactive)\n"
        "- /listschedules [channel_id] — List schedules for a channel\n"
        "- /editschedule <schedule_id> — Edit a schedule (interactive)\n"
        "- /setscheduletimezone [schedule_id] <timezone> — Set a schedule timezone\n"
        "- /pauseschedule [schedule_id]\n"
        "- /resumeschedule [schedule_id]\n"
        "- /deleteschedule [schedule_id]\n"
        "- /copyschedule <schedule_id> <target_channel_id>\n"
        "\n"
        "Queue:\n"
        "- /viewqueue [schedule_id] [count]\n"
        "- /deletepost <post_id>\n"
        "- /testschedule [schedule_id] [run_count]\n"
        "\n"
        "Bulk upload:\n"
        "- /bulk [schedule_id]\n"
        "- /done (inside bulk upload)\n"
        "- /cancel\n"
    )


def _onboarding_segments() -> list[Segment]:
    return [
        Segment("Quick start:\n"),
        Segment("Tip: set your default timezone with "),
        Segment("/settimezone"),
        Segment(" (example: "),
        Segment("Europe/Amsterdam", code=True),
        Segment("). New schedules will interpret times in that timezone.\n\n"),
        Segment("1) Add this bot to your channel as an administrator (with permission to post messages)\n"),
        Segment("2) In the channel, post "),
        Segment("/channelid"),
        Segment(" to show the numeric channel id\n"),
        Segment("3) In private chat, run "),
        Segment("/addchannel"),
        Segment(" <channel_id> and post the verification code to the channel\n"),
        Segment("4) Optional (recommended): set defaults with "),
        Segment("/selectchannel"),
        Segment(" and "),
        Segment("/selectschedule"),
        Segment(" (check with "),
        Segment("/selection"),
        Segment(")\n"),
        Segment("5) Create schedules with "),
        Segment("/newschedule"),
        Segment(" and queue posts with "),
        Segment("/bulk"),
        Segment("\n\nOptional: configure forwarding allowlist with "),
        Segment("/forwarding"),
        Segment(" to preserve 'Forwarded from ...' attribution for selected source channels.\n"),
        Segment("Forwarding is only applied during /bulk when caption mode is "),
        Segment("preserve", code=True),
        Segment("."),
        Segment("\n"),
    ]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with command overview."""
    await ensure_user_record(update, context)

    if update.message is None:
        return

    try:
        details = await db.get_user_context_details(update.effective_user.id) if update.effective_user else {}

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
        user_id = update.effective_user.id if update.effective_user else None
        logger.info("Handled /start for user_id=%s", user_id)
    except Exception as e:
        user_id = update.effective_user.id if update.effective_user else None
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

