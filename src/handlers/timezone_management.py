"""User timezone preference commands."""

from __future__ import annotations

import logging
import os
from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def _default_timezone_name() -> str:
    return os.getenv("DEFAULT_TIMEZONE", "UTC") or "UTC"


def _is_valid_timezone(tz_name: str) -> bool:
    if tz_name.upper() in {"UTC", "ETC/UTC"}:
        return True
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


async def gettimezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's configured timezone (or default)."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    configured = await db.get_user_timezone(update.effective_user.id)
    effective = configured or _default_timezone_name()

    segments: list[Segment] = [
        Segment("Your timezone: "),
        Segment(effective, code=True),
    ]
    if not configured:
        segments += [
            Segment(" (default)\n"),
            Segment("Set it with "),
            Segment("/settimezone"),
            Segment(" using an IANA timezone name like "),
            Segment("Europe/Amsterdam", code=True),
            Segment(", "),
            Segment("UTC", code=True),
            Segment(", or "),
            Segment("UTC", code=True),
            Segment("."),
        ]
    else:
        segments += [
            Segment("\nChange it with "),
            Segment("/settimezone"),
            Segment(" or reset to default with "),
            Segment("/settimezone default"),
            Segment("."),
        ]

    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def settimezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the user's default timezone for new schedules."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        text, entities = render(
            [
                Segment("Usage: "),
                Segment("/settimezone"),
                Segment(" "),
                Segment("<timezone>", code=True),
                Segment("\nExample: "),
                Segment("/settimezone"),
                Segment(" "),
                Segment("Europe/Amsterdam", code=True),
                Segment("\nReset: "),
                Segment("/settimezone default"),
            ]
        )
        await update.message.reply_text(text, entities=entities)
        return

    raw = (context.args[0] or "").strip()
    lowered = raw.lower()
    if lowered in {"default", "clear", "reset"}:
        await db.set_user_timezone(update.effective_user.id, None)
        effective = _default_timezone_name()
        text, entities = render(
            [
                Segment("Timezone cleared. Using default: "),
                Segment(effective, code=True),
                Segment("."),
            ]
        )
        await update.message.reply_text(text, entities=entities)
        return

    if not _is_valid_timezone(raw):
        text, entities = render(
            [
                Segment("Unknown timezone: "),
                Segment(raw, code=True),
                Segment("\nUse an IANA timezone name like "),
                Segment("Europe/Amsterdam", code=True),
                Segment(", "),
                Segment("UTC", code=True),
                Segment("."),
            ]
        )
        await update.message.reply_text(text, entities=entities)
        return

    await db.set_user_timezone(update.effective_user.id, raw)
    text, entities = render(
        [
            Segment("Timezone set to "),
            Segment(raw, code=True),
            Segment(".\nThis will be used as the default timezone for new schedules."),
        ]
    )
    await update.message.reply_text(text, entities=entities)
    logger.info("User %s set timezone to %r", update.effective_user.id, raw)

