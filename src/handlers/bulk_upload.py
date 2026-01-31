"""Bulk upload conversation for queueing posts to a schedule."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from telegram import Message, MessageEntity, Update
from telegram.constants import ChatType
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import queries as db
from handlers.common import ensure_user_record
from handlers.selection import selection_segments
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


SELECTING_CAPTION_MODE, WAITING_SINGLE_CAPTION, COLLECTING_MEDIA, CONFIRMING = range(4)


@dataclass(frozen=True)
class _CollectedItem:
    """A single collected media item (for media groups)."""

    media_type: str
    file_id: str
    caption: str | None
    caption_entities: list[dict[str, Any]] | None


def _state_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("bulk_"):
            context.user_data.pop(key, None)


def _get_caption_mode(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    mode = context.user_data.get("bulk_caption_mode")
    if mode in {"preserve", "remove", "single"}:
        return str(mode)
    return None


def _get_single_caption_entities(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]] | None:
    value = context.user_data.get("bulk_single_caption_entities")
    if isinstance(value, list):
        return value
    return None


def _get_single_caption(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    value = context.user_data.get("bulk_single_caption")
    if value is None:
        return None
    return str(value)


def _entities_to_dicts(entities: list[MessageEntity] | None) -> list[dict[str, Any]] | None:
    if not entities:
        return None
    return [e.to_dict() for e in entities]


def _utf16_len(text: str) -> int:
    # Telegram entity offsets/lengths are in UTF-16 code units.
    return len(text.encode("utf-16-le")) // 2


def _parse_markdownish(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Parse a small, user-friendly markdown subset into Telegram entities.

    Supported:
    - Inline links: [text](url) -> text_link entity
    - Inline code: `code` -> code entity

    If nothing is parsed, returns original text and None.
    """
    out: list[str] = []
    entities: list[dict[str, Any]] = []
    i = 0
    out_utf16 = 0
    md_escapable = set("_*[]()~`>#+-=|{}.!\\")

    def _append_plain(s: str) -> None:
        nonlocal out_utf16
        if not s:
            return
        out.append(s)
        out_utf16 += _utf16_len(s)

    while i < len(text):
        ch = text[i]

        # Treat backslash-escaped MarkdownV2 characters as literals.
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in md_escapable:
            _append_plain(text[i + 1])
            i += 2
            continue

        # Inline code: `...`
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1:
                code_text = text[i + 1 : j]
                start = out_utf16
                _append_plain(code_text)
                length = _utf16_len(code_text)
                if length:
                    entities.append({"type": "code", "offset": start, "length": length})
                i = j + 1
                continue

        # Inline link: [text](url)
        if ch == "[":
            close_bracket = text.find("]", i + 1)
            if close_bracket != -1 and close_bracket + 1 < len(text) and text[close_bracket + 1] == "(":
                close_paren = text.find(")", close_bracket + 2)
                if close_paren != -1:
                    link_text = text[i + 1 : close_bracket]
                    url = text[close_bracket + 2 : close_paren]
                    start = out_utf16
                    _append_plain(link_text)
                    length = _utf16_len(link_text)
                    if length and url:
                        entities.append({"type": "text_link", "offset": start, "length": length, "url": url})
                    i = close_paren + 1
                    continue

        _append_plain(ch)
        i += 1

    out_text = "".join(out)
    return (out_text, entities or None)


def _get_posts(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    posts = context.user_data.get("bulk_posts")
    if isinstance(posts, list):
        return posts
    posts = []
    context.user_data["bulk_posts"] = posts
    return posts


def _get_media_groups(context: ContextTypes.DEFAULT_TYPE) -> dict[str, list[_CollectedItem]]:
    groups = context.user_data.get("bulk_media_groups")
    if isinstance(groups, dict):
        return groups
    groups = {}
    context.user_data["bulk_media_groups"] = groups
    return groups


def _get_media_group_indexes(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    """Map media_group_id -> index in bulk_posts for stable ordering."""
    indexes = context.user_data.get("bulk_media_group_indexes")
    if isinstance(indexes, dict):
        return indexes
    indexes = {}
    context.user_data["bulk_media_group_indexes"] = indexes
    return indexes


def _message_to_collected_item(
    message: Message,
    *,
    caption_mode: str,
    single_caption: str | None,
    single_caption_entities: list[dict[str, Any]] | None,
) -> _CollectedItem | None:
    caption: str | None
    caption_entities: list[dict[str, Any]] | None
    if caption_mode == "remove":
        caption = None
        caption_entities = None
    elif caption_mode == "single":
        caption = single_caption
        caption_entities = single_caption_entities
    else:
        caption = message.caption or None
        caption_entities = _entities_to_dicts(getattr(message, "caption_entities", None))
        if caption is None and caption_entities:
            caption_entities = None

    if message.photo:
        file_id = message.photo[-1].file_id
        return _CollectedItem(media_type="photo", file_id=file_id, caption=caption, caption_entities=caption_entities)

    if message.video:
        return _CollectedItem(media_type="video", file_id=message.video.file_id, caption=caption, caption_entities=caption_entities)

    if message.document:
        return _CollectedItem(media_type="document", file_id=message.document.file_id, caption=caption, caption_entities=caption_entities)

    return None


def _finalize_media_group_items(
    items: list[_CollectedItem],
    *,
    caption_mode: str,
    single_caption: str | None,
    single_caption_entities: list[dict[str, Any]] | None,
) -> str:
    """Convert collected items into media_group_data JSON."""
    if not items:
        raise ValueError("Empty media group")

    # Determine group caption behavior
    group_caption: str | None
    group_caption_entities: list[dict[str, Any]] | None
    if caption_mode == "remove":
        group_caption = None
        group_caption_entities = None
    elif caption_mode == "single":
        group_caption = single_caption
        group_caption_entities = single_caption_entities
    else:
        first = next((i for i in items if i.caption), None)
        group_caption = first.caption if first else None
        group_caption_entities = first.caption_entities if first else None

    result: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        result.append(
            {
                "media_type": item.media_type,
                "file_id": item.file_id,
                "file_path": None,
                "caption": group_caption if idx == 0 else None,
                "caption_parse_mode": None,
                "caption_entities": group_caption_entities if idx == 0 else None,
            }
        )

    return json.dumps(result)


async def bulk_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /bulk <schedule_id>."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please run /bulk in a private chat with the bot.")
        return ConversationHandler.END

    schedule_id: int | None = None
    used_selected = False
    if context.args and len(context.args) == 1:
        try:
            schedule_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid schedule id.")
            return ConversationHandler.END
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /bulk <schedule_id>\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
        )
        return ConversationHandler.END

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return ConversationHandler.END

    if not used_selected:
        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(schedule["channel_id"]),
            selected_schedule_id=schedule_id,
        )

    _state_clear(context)
    context.user_data["bulk_schedule_id"] = schedule_id

    details = await db.get_user_context_details(update.effective_user.id)
    segments = [
        Segment("Bulk upload started for schedule "),
        Segment(str(schedule_id), code=True),
        Segment(".\n\n"),
        *selection_segments(details),
        Segment("\n\nChoose caption mode by replying with one of:\n"),
        Segment("- preserve (keep original captions)\n"),
        Segment("- remove (remove all captions)\n"),
        Segment("- single (use one caption for all posts; formatting is preserved)\n\n"),
        Segment("Tip: for 'single', you can format the caption message (links/code/etc) and the bot will keep it.\n"),
        Segment("Tip: you can also paste [text](url) links and `inline code` and it will be preserved.\n\n"),
        Segment("Or /cancel to stop."),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)
    return SELECTING_CAPTION_MODE


async def bulk_set_caption_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    raw = (update.message.text or "").strip().lower()
    # Backwards-compatible aliases (older prompts used these).
    if raw in {"markdown", "markdownv2", "md", "md2", "html"}:
        raw = "single"
    if raw not in {"preserve", "remove", "single"}:
        await update.message.reply_text("Invalid caption mode. Reply with: preserve, remove, single")
        return SELECTING_CAPTION_MODE

    context.user_data["bulk_caption_mode"] = raw
    if raw == "single":
        await update.message.reply_text("Send the single caption to apply to all posts.")
        return WAITING_SINGLE_CAPTION

    await update.message.reply_text(
        "Caption mode set.\n"
        "Now send photos, videos, or documents.\n"
        "When you're done, send /done to queue them or /cancel to stop."
    )
    return COLLECTING_MEDIA


async def bulk_set_single_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    raw_text = update.message.text or ""
    if not raw_text.strip():
        await update.message.reply_text("Caption cannot be empty. Send a caption, or /cancel.")
        return WAITING_SINGLE_CAPTION

    # Prefer Telegram-native formatting if present.
    entities_dicts = _entities_to_dicts(getattr(update.message, "entities", None))
    if entities_dicts:
        caption_text = raw_text
        caption_entities = entities_dicts
    else:
        # Otherwise, parse a small markdown subset into entities.
        caption_text, caption_entities = _parse_markdownish(raw_text)

    context.user_data["bulk_single_caption"] = caption_text
    if caption_entities:
        context.user_data["bulk_single_caption_entities"] = caption_entities
    else:
        context.user_data.pop("bulk_single_caption_entities", None)

    await update.message.reply_text(
        "Caption saved.\n"
        "Now send photos, videos, or documents.\n"
        "When you're done, send /done to queue them or /cancel to stop."
    )
    return COLLECTING_MEDIA


async def bulk_collect_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect media messages into an in-memory list until /done."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    # If user sends media while in the confirm step, accept it and return to collecting.
    # This avoids losing late-arriving media-group items.
    if context.user_data.get("bulk_in_confirming") is True:
        context.user_data["bulk_in_confirming"] = False

    caption_mode = _get_caption_mode(context)
    if caption_mode is None:
        await update.message.reply_text("Caption mode missing. Restart with /bulk <schedule_id>.")
        return ConversationHandler.END

    single_caption = _get_single_caption(context)
    if caption_mode == "single" and not single_caption:
        await update.message.reply_text("Single caption missing. Restart with /bulk <schedule_id>.")
        return ConversationHandler.END

    single_caption_entities = _get_single_caption_entities(context) if caption_mode == "single" else None

    item = _message_to_collected_item(
        update.message,
        caption_mode=caption_mode,
        single_caption=single_caption,
        single_caption_entities=single_caption_entities,
    )
    if item is None:
        await update.message.reply_text("Unsupported message type. Send a photo, video, or document.")
        return COLLECTING_MEDIA

    # Media groups: buffer until group is complete.
    group_id = update.message.media_group_id
    if group_id:
        groups = _get_media_groups(context)
        groups.setdefault(group_id, []).append(item)

        posts = _get_posts(context)
        indexes = _get_media_group_indexes(context)
        if group_id not in indexes:
            indexes[group_id] = len(posts)
            posts.append(
                {
                    "media_type": "media_group",
                    "file_id": None,
                    "file_path": None,
                    "caption": None,
                    "caption_parse_mode": None,
                    "caption_entities": json.dumps(single_caption_entities) if single_caption_entities else None,
                    "media_group_data": None,
                }
            )

        await update.message.reply_text(
            f"Added media group item. Total collected posts: {len(posts)}.\n"
            "Send more, or /done to finish."
        )
        return COLLECTING_MEDIA

    posts = _get_posts(context)
    posts.append(
        {
            "media_type": item.media_type,
            "file_id": item.file_id,
            "file_path": None,
            "caption": item.caption,
            "caption_parse_mode": None,
            "caption_entities": json.dumps(item.caption_entities) if item.caption_entities else None,
            "media_group_data": None,
        }
    )

    await update.message.reply_text(
        f"Added {item.media_type}. Total collected posts: {len(posts)}.\n"
        "Send more, or /done to finish."
    )
    return COLLECTING_MEDIA


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE, *, group_id: str) -> None:
    caption_mode = _get_caption_mode(context)
    if caption_mode is None:
        return

    single_caption = _get_single_caption(context)
    single_caption_entities = _get_single_caption_entities(context) if caption_mode == "single" else None

    groups = _get_media_groups(context)
    items = groups.pop(group_id, [])
    if not items:
        return

    indexes = _get_media_group_indexes(context)
    idx = indexes.pop(group_id, None)

    posts = _get_posts(context)
    media_group_data = _finalize_media_group_items(
        items,
        caption_mode=caption_mode,
        single_caption=single_caption,
        single_caption_entities=single_caption_entities,
    )
    if idx is None or idx >= len(posts):
        posts.append(
            {
                "media_type": "media_group",
                "file_id": None,
                "file_path": None,
                "caption": None,
                "caption_parse_mode": None,
                "caption_entities": json.dumps(single_caption_entities) if single_caption_entities else None,
                "media_group_data": media_group_data,
            }
        )
        return

    posts[idx]["media_type"] = "media_group"
    posts[idx]["file_id"] = None
    posts[idx]["file_path"] = None
    posts[idx]["caption"] = None
    posts[idx]["caption_parse_mode"] = None
    posts[idx]["caption_entities"] = json.dumps(single_caption_entities) if single_caption_entities else None
    posts[idx]["media_group_data"] = media_group_data


async def bulk_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalize collection and ask for confirmation."""
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    # Flush any remaining media groups immediately.
    groups = list(_get_media_groups(context).keys())
    for gid in groups:
        await _flush_media_group(context, group_id=gid)

    posts = _get_posts(context)
    if not posts:
        await update.message.reply_text("No posts collected yet. Send media, then /done.")
        return COLLECTING_MEDIA

    counts: dict[str, int] = {}
    for p in posts:
        counts[p["media_type"]] = counts.get(p["media_type"], 0) + 1

    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    schedule_id = context.user_data.get("bulk_schedule_id")

    details = await db.get_user_context_details(update.effective_user.id if update.effective_user else 0)
    segments = [
        Segment(f"Ready to queue {len(posts)} posts for schedule "),
        Segment(str(schedule_id), code=True),
        Segment(".\n"),
        Segment(f"Breakdown: {', '.join(parts)}\n\n"),
        Segment("Reply 'yes' to confirm, or 'no' to cancel.\n\n"),
        *selection_segments(details),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)
    context.user_data["bulk_in_confirming"] = True
    return CONFIRMING


async def bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    text = (update.message.text or "").strip().lower()
    if text not in {"yes", "no"}:
        await update.message.reply_text("Reply 'yes' to confirm or 'no' to cancel.")
        return CONFIRMING

    if text == "no":
        _state_clear(context)
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    schedule_id_raw = context.user_data.get("bulk_schedule_id")
    if schedule_id_raw is None:
        await update.message.reply_text("Missing schedule id. Restart with /bulk <schedule_id>.")
        return ConversationHandler.END

    schedule_id = int(schedule_id_raw)

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        _state_clear(context)
        await update.message.reply_text("Schedule not found or not owned by you.")
        return ConversationHandler.END

    posts = _get_posts(context)
    inserted = await db.add_queued_posts_bulk(schedule_id, posts)

    # If it was empty_paused, it is no longer empty; keep it paused.
    if schedule.get("state") == "empty_paused":
        await db.update_schedule_state(schedule_id, "paused")

    _state_clear(context)
    details = await db.get_user_context_details(update.effective_user.id)
    segments = [
        Segment(f"Queued {inserted} posts.\n"),
        Segment("Use /resumeschedule "),
        Segment(str(schedule_id), code=True),
        Segment(" to start posting.\n\n"),
        *selection_segments(details),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)
    logger.info("User %s queued %s posts for schedule id=%s", update.effective_user.id, inserted, schedule_id)
    return ConversationHandler.END


async def bulk_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    _state_clear(context)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


bulk_upload_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("bulk", bulk_start)],
    states={
        SELECTING_CAPTION_MODE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_set_caption_mode),
        ],
        WAITING_SINGLE_CAPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_set_single_caption),
        ],
        COLLECTING_MEDIA: [
            MessageHandler(
                filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
                bulk_collect_media,
            ),
            CommandHandler("done", bulk_done),
            CommandHandler("cancel", bulk_cancel),
        ],
        CONFIRMING: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_confirm),
            MessageHandler(
                filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
                bulk_collect_media,
            ),
        ],
    },
    fallbacks=[CommandHandler("cancel", bulk_cancel)],
)

