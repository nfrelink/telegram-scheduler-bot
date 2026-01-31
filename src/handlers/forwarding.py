"""Per-user forwarding configuration commands."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from utils.tg_text import Segment, render


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


async def forwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current forwarding allowlist for the user."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    origins = await db.get_forward_origin_allowlist(update.effective_user.id)
    if not origins:
        segments = [
            Segment("Forwarding allowlist is empty.\n\n"),
            Segment("When you use /bulk with caption mode 'preserve', forwarded messages from allowlisted channels\n"),
            Segment("will be forwarded into your destination channel (preserving 'Forwarded from ...').\n\n"),
            Segment("Add one: /addforward <origin_channel_id>\n"),
            Segment("Remove one: /removeforward <origin_channel_id>\n"),
            Segment("Clear all: /clearforward\n"),
        ]
        text, entities = render(segments)
        await update.message.reply_text(text, entities=entities)
        return

    segments: list[Segment] = [
        Segment("Forwarding allowlist (origin channel IDs):\n"),
        Segment("Used only with /bulk when caption mode is 'preserve'.\n\n"),
    ]
    for cid in origins:
        segments += [Segment("- "), Segment(str(cid), code=True), Segment("\n")]

    segments += [
        Segment("\nAdd one: /addforward <origin_channel_id>\n"),
        Segment("Remove one: /removeforward <origin_channel_id>\n"),
        Segment("Clear all: /clearforward\n"),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def addforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a channel ID to the user's forwarding allowlist."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /addforward <origin_channel_id>")
        return

    origin = _parse_int(context.args[0])
    if origin is None:
        await update.message.reply_text("Invalid origin_channel_id.")
        return

    await db.add_forward_origin_allowlist(user_id=update.effective_user.id, origin_chat_id=origin)
    text, entities = render([Segment("Added "), Segment(str(origin), code=True), Segment(" to forwarding allowlist.")])
    await update.message.reply_text(text, entities=entities)


async def removeforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a channel ID from the user's forwarding allowlist."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /removeforward <origin_channel_id>")
        return

    origin = _parse_int(context.args[0])
    if origin is None:
        await update.message.reply_text("Invalid origin_channel_id.")
        return

    await db.remove_forward_origin_allowlist(user_id=update.effective_user.id, origin_chat_id=origin)
    text, entities = render([Segment("Removed "), Segment(str(origin), code=True), Segment(" from forwarding allowlist.")])
    await update.message.reply_text(text, entities=entities)


async def clearforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the user's forwarding allowlist."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    await db.clear_forward_origin_allowlist(update.effective_user.id)
    await update.message.reply_text("Forwarding allowlist cleared.")

