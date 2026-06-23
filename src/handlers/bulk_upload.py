"""Bulk upload conversation for queueing posts to a schedule."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ChatType
from telegram.error import NetworkError, TimedOut
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
from handlers.selection import selection_segments
from services import dedup, posting, scheduling
from utils.image_hash import compute_dhash
from utils.tg_text import Segment, render, utf16_len

logger = logging.getLogger(__name__)

_CAPTION_PREVIEW_LEN = 80
_MD_ESCAPABLE_CHARS = set("_*[]()~`>#+-=|{}.!\\")
_MEDIA_GROUP_ERROR_MSG = (
    "Something went wrong preparing your media groups. Please start over with /bulk."
)
_SINGLE_CAPTION_FORMAT_TIP = (
    "Tip: for 'single', formatting is preserved. "
    "You can use [text](url) links and `inline code`.\n\n"
)


(
    SELECTING_CAPTION_MODE,
    WAITING_SINGLE_CAPTION,
    COLLECTING_MEDIA,
    CONFIRMING,
    DECIDING_SPLITS,
    RESUMING,
) = range(6)


@dataclass(frozen=True)
class _CollectedItem:
    """A single collected media item (for media groups)."""

    media_type: str
    file_id: str
    file_unique_id: str
    caption: str | None
    caption_entities: list[dict[str, Any]] | None
    forward_from_chat_id: int | None
    forward_from_message_id: int | None
    forward_origin_chat_id: int | None
    forward_origin_message_id: int | None
    # The raw channel that the message was forwarded from, regardless of the
    # allowlist.  Used to decide whether to show the album-split prompt.
    raw_origin_chat_id: int | None = None
    # Message ID in the origin channel, used to build a t.me deep link.
    raw_origin_message_id: int | None = None
    # True if the message was forwarded from any source (channel or person).
    # Distinct from raw_origin_chat_id which is only set for channel forwards.
    raw_origin_is_forwarded: bool = False
    # Telegram message_id of the user's message, for reply-to in decision prompts.
    user_message_id: int | None = None


def _state_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("bulk_"):
            context.user_data.pop(key, None)


async def _clear_staging(user_id: int) -> None:
    """Remove all staging data for a user (items + session)."""
    await db.clear_staging(user_id)


async def _persist_item_to_staging(
    user_id: int, item: _CollectedItem, *, media_group_id: str | None = None
) -> None:
    """Write-through: persist a collected item to the staging table."""
    await db.add_staging_item(
        user_id,
        media_type=item.media_type,
        file_id=item.file_id,
        caption=item.caption,
        caption_entities=json.dumps(item.caption_entities) if item.caption_entities else None,
        media_group_id=media_group_id,
        forward_from_chat_id=item.forward_from_chat_id,
        forward_from_message_id=item.forward_from_message_id,
        forward_origin_chat_id=item.forward_origin_chat_id,
        forward_origin_message_id=item.forward_origin_message_id,
        raw_origin_chat_id=item.raw_origin_chat_id,
        raw_origin_message_id=item.raw_origin_message_id,
        raw_origin_is_forwarded=item.raw_origin_is_forwarded,
    )


def _load_staging_into_user_data(
    context: ContextTypes.DEFAULT_TYPE,
    items: list[dict[str, Any]],
    session: dict[str, Any],
) -> None:
    """Reconstruct user_data from staging DB rows + session metadata."""
    context.user_data["bulk_schedule_id"] = session["schedule_id"]
    context.user_data["bulk_caption_mode"] = session["caption_mode"]
    if session.get("single_caption"):
        context.user_data["bulk_single_caption"] = session["single_caption"]
    if session.get("single_caption_entities"):
        raw = session["single_caption_entities"]
        context.user_data["bulk_single_caption_entities"] = (
            json.loads(raw) if isinstance(raw, str) else raw
        )

    posts = _get_posts(context)
    groups = _get_media_groups(context)
    indexes = _get_media_group_indexes(context)

    for row in items:
        mg_id = row.get("media_group_id")
        if mg_id:
            cap_ents = row.get("caption_entities")
            if isinstance(cap_ents, str):
                cap_ents = json.loads(cap_ents)
            collected = _CollectedItem(
                media_type=row["media_type"],
                file_id=row.get("file_id") or "",
                file_unique_id=row.get("file_unique_id") or "",
                caption=row.get("caption"),
                caption_entities=cap_ents,
                forward_from_chat_id=row.get("forward_from_chat_id"),
                forward_from_message_id=row.get("forward_from_message_id"),
                forward_origin_chat_id=row.get("forward_origin_chat_id"),
                forward_origin_message_id=row.get("forward_origin_message_id"),
                raw_origin_chat_id=row.get("raw_origin_chat_id"),
                raw_origin_message_id=row.get("raw_origin_message_id"),
                raw_origin_is_forwarded=bool(row.get("raw_origin_is_forwarded")),
            )
            groups.setdefault(mg_id, []).append(collected)
            if mg_id not in indexes:
                indexes[mg_id] = len(posts)
                single_cap_ents = context.user_data.get("bulk_single_caption_entities")
                posts.append(
                    {
                        "media_type": "media_group",
                        "file_id": None,
                        "file_path": None,
                        "caption": None,
                        "caption_parse_mode": None,
                        "caption_entities": json.dumps(single_cap_ents)
                        if single_cap_ents
                        else None,
                        "media_group_data": None,
                    }
                )
        else:
            cap_ents_str = row.get("caption_entities")
            posts.append(
                {
                    "media_type": row["media_type"],
                    "file_id": row.get("file_id"),
                    "file_path": None,
                    "caption": row.get("caption"),
                    "caption_parse_mode": None,
                    "caption_entities": cap_ents_str,
                    "forward_from_chat_id": row.get("forward_from_chat_id"),
                    "forward_from_message_id": row.get("forward_from_message_id"),
                    "forward_origin_chat_id": row.get("forward_origin_chat_id"),
                    "forward_origin_message_id": row.get("forward_origin_message_id"),
                    "media_group_data": None,
                }
            )


def _get_caption_mode(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    mode = context.user_data.get("bulk_caption_mode")
    if mode in {"remove", "single", "preserve"}:
        return str(mode)
    return None


def _get_single_caption_entities(
    context: ContextTypes.DEFAULT_TYPE,
) -> list[dict[str, Any]] | None:
    value = context.user_data.get("bulk_single_caption_entities")
    if isinstance(value, list):
        return value
    return None


def _get_single_caption(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    value = context.user_data.get("bulk_single_caption")
    if value is None:
        return None
    return str(value)


def _entities_to_dicts(
    entities: list[MessageEntity] | None,
) -> list[dict[str, Any]] | None:
    if not entities:
        return None
    return [e.to_dict() for e in entities]


def _extract_forward_origin_channel(message: Message) -> tuple[int | None, int | None]:
    """Return (origin_chat_id, origin_message_id) if message was forwarded from a channel."""
    # Bot API legacy fields (still present in PTB for compatibility).
    fwd_chat = getattr(message, "forward_from_chat", None)
    fwd_msg_id = getattr(message, "forward_from_message_id", None)
    if fwd_chat is not None and getattr(fwd_chat, "type", None) == ChatType.CHANNEL:
        chat_id_raw = getattr(fwd_chat, "id", None)
        if chat_id_raw is not None and fwd_msg_id is not None:
            try:
                return int(chat_id_raw), int(fwd_msg_id)
            except TypeError, ValueError:
                return None, None

    # Bot API v7+ origin object.
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin is not None else None
    origin_message_id = getattr(origin, "message_id", None) if origin is not None else None
    if (
        origin_chat is not None
        and origin_message_id is not None
        and getattr(origin_chat, "type", None) == ChatType.CHANNEL
    ):
        chat_id_raw = getattr(origin_chat, "id", None)
        if chat_id_raw is not None:
            try:
                return int(chat_id_raw), int(origin_message_id)
            except TypeError, ValueError:
                return None, None

    return None, None


def _is_forwarded_message(message: Message) -> bool:
    """Return True if the message was forwarded from any source (channel or person)."""
    if getattr(message, "forward_from_chat", None) is not None:
        return True
    if getattr(message, "forward_from", None) is not None:
        return True
    return getattr(message, "forward_origin", None) is not None


@dataclass
class _MarkdownParseState:
    out: list[str] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    out_utf16: int = 0

    def append_plain(self, text: str) -> None:
        if not text:
            return
        self.out.append(text)
        self.out_utf16 += utf16_len(text)


def _md_try_consume_escape(text: str, index: int, state: _MarkdownParseState) -> int | None:
    """Consume a backslash-escaped MarkdownV2 character; return new index or None."""
    if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in _MD_ESCAPABLE_CHARS:
        state.append_plain(text[index + 1])
        return index + 2
    return None


def _md_try_consume_inline_code(text: str, index: int, state: _MarkdownParseState) -> int | None:
    """Consume an inline `code` span; return new index or None."""
    if text[index] != "`":
        return None
    close = text.find("`", index + 1)
    if close == -1:
        return None
    code_text = text[index + 1 : close]
    start = state.out_utf16
    state.append_plain(code_text)
    length = utf16_len(code_text)
    if length:
        state.entities.append({"type": "code", "offset": start, "length": length})
    return close + 1


def _md_try_consume_inline_link(text: str, index: int, state: _MarkdownParseState) -> int | None:
    """Consume an inline [text](url) link; return new index or None."""
    if text[index] != "[":
        return None
    close_bracket = text.find("]", index + 1)
    if close_bracket == -1 or close_bracket + 1 >= len(text) or text[close_bracket + 1] != "(":
        return None
    close_paren = text.find(")", close_bracket + 2)
    if close_paren == -1:
        return None
    link_text = text[index + 1 : close_bracket]
    url = text[close_bracket + 2 : close_paren]
    start = state.out_utf16
    state.append_plain(link_text)
    length = utf16_len(link_text)
    if length and url:
        state.entities.append(
            {
                "type": "text_link",
                "offset": start,
                "length": length,
                "url": url,
            }
        )
    return close_paren + 1


def _parse_markdownish(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Parse a small, user-friendly markdown subset into Telegram entities.

    Supported:
    - Inline links: [text](url) -> text_link entity
    - Inline code: `code` -> code entity

    If nothing is parsed, returns original text and None.
    """
    state = _MarkdownParseState()
    index = 0
    while index < len(text):
        next_index = (
            _md_try_consume_escape(text, index, state)
            or _md_try_consume_inline_code(text, index, state)
            or _md_try_consume_inline_link(text, index, state)
        )
        if next_index is not None:
            index = next_index
            continue
        state.append_plain(text[index])
        index += 1

    out_text = "".join(state.out)
    return (out_text, state.entities or None)


def _get_posts(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    posts = context.user_data.get("bulk_posts")
    if isinstance(posts, list):
        return posts
    posts = []
    context.user_data["bulk_posts"] = posts
    return posts


def _get_media_groups(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, list[_CollectedItem]]:
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
    forward_origin_allowlist: set[int] | None,
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

    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None
    forward_origin_chat_id: int | None = None
    forward_origin_message_id: int | None = None

    # Always extract origin so we can detect forwarded albums later, even when
    # the source channel is not on the allowlist.
    raw_origin_chat_id, raw_origin_msg_id = _extract_forward_origin_channel(message)
    raw_origin_is_forwarded = _is_forwarded_message(message)

    allow = forward_origin_allowlist or set()
    if allow and raw_origin_chat_id is not None and raw_origin_chat_id in allow:
        chat = getattr(message, "chat", None)
        msg_id = getattr(message, "message_id", None)
        chat_id_raw = getattr(chat, "id", None) if chat is not None else None
        if chat_id_raw is not None and msg_id is not None:
            try:
                forward_from_chat_id = int(chat_id_raw)
                forward_from_message_id = int(msg_id)
                forward_origin_chat_id = int(raw_origin_chat_id)
                forward_origin_message_id = (
                    int(raw_origin_msg_id) if raw_origin_msg_id is not None else None
                )
            except TypeError, ValueError:
                pass

    msg_id = getattr(message, "message_id", None)

    if message.photo:
        largest = message.photo[-1]
        return _CollectedItem(
            media_type="photo",
            file_id=largest.file_id,
            file_unique_id=largest.file_unique_id,
            caption=caption,
            caption_entities=caption_entities,
            forward_from_chat_id=forward_from_chat_id,
            forward_from_message_id=forward_from_message_id,
            forward_origin_chat_id=forward_origin_chat_id,
            forward_origin_message_id=forward_origin_message_id,
            raw_origin_chat_id=raw_origin_chat_id,
            raw_origin_message_id=raw_origin_msg_id,
            raw_origin_is_forwarded=raw_origin_is_forwarded,
            user_message_id=msg_id,
        )

    if message.video:
        return _CollectedItem(
            media_type="video",
            file_id=message.video.file_id,
            file_unique_id=message.video.file_unique_id,
            caption=caption,
            caption_entities=caption_entities,
            forward_from_chat_id=forward_from_chat_id,
            forward_from_message_id=forward_from_message_id,
            forward_origin_chat_id=forward_origin_chat_id,
            forward_origin_message_id=forward_origin_message_id,
            raw_origin_chat_id=raw_origin_chat_id,
            raw_origin_message_id=raw_origin_msg_id,
            raw_origin_is_forwarded=raw_origin_is_forwarded,
            user_message_id=msg_id,
        )

    if message.document:
        return _CollectedItem(
            media_type="document",
            file_id=message.document.file_id,
            file_unique_id=message.document.file_unique_id,
            caption=caption,
            caption_entities=caption_entities,
            forward_from_chat_id=forward_from_chat_id,
            forward_from_message_id=forward_from_message_id,
            forward_origin_chat_id=forward_origin_chat_id,
            forward_origin_message_id=forward_origin_message_id,
            raw_origin_chat_id=raw_origin_chat_id,
            raw_origin_message_id=raw_origin_msg_id,
            raw_origin_is_forwarded=raw_origin_is_forwarded,
            user_message_id=msg_id,
        )

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

    # Best-effort stable ordering: if we have per-item message IDs (for forwarding),
    # order by them so the album forwards in the same order as received.
    items = sorted(
        items,
        key=lambda i: (
            i.forward_from_message_id is None,
            int(i.forward_from_message_id or 0),
        ),
    )

    # Determine group caption behavior
    group_caption: str | None
    group_caption_entities: list[dict[str, Any]] | None
    if caption_mode == "single":
        group_caption = single_caption
        group_caption_entities = single_caption_entities
    else:  # remove
        group_caption = None
        group_caption_entities = None

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
                "forward_from_chat_id": item.forward_from_chat_id,
                "forward_from_message_id": item.forward_from_message_id,
                "forward_origin_chat_id": item.forward_origin_chat_id,
                "forward_origin_message_id": item.forward_origin_message_id,
            }
        )

    return json.dumps(result)


def _group_needs_split_prompt(items: list[_CollectedItem], allowlist: set[int]) -> bool:
    """True if the group should trigger the keep-or-split prompt.

    Only forwarded albums whose origin is NOT on the allowlist need a decision.
    Locally uploaded albums and allowlisted-channel albums are always handled
    silently (kept as album or forwarded natively, respectively).

    Person-forwarded albums (forward_from set, no channel origin) also trigger
    the prompt — they can never be on the channel allowlist.
    """
    if not items:
        return False
    first = items[0]
    if not first.raw_origin_is_forwarded:
        return False  # locally uploaded — keep as album without prompting
    return not (first.raw_origin_chat_id is not None and first.raw_origin_chat_id in allowlist)


def _origin_link(chat_id: int | None, message_id: int | None) -> str | None:
    """Return a t.me deep link for a channel message, or None if not possible.

    Only works for supergroup/channel IDs in the -100XXXXXXXXXX format.
    """
    if not chat_id or not message_id:
        return None
    chat_str = str(chat_id)
    if not chat_str.startswith("-100"):
        return None
    inner_id = chat_str[4:]  # strip the "-100" prefix
    return f"https://t.me/c/{inner_id}/{message_id}"


def _build_split_prompt(
    count: int,
    placeholder_idx: int,
    *,
    album_num: int,
    total_albums: int,
    first_caption: str | None = None,
    origin_link: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the text and inline keyboard for a single split decision."""
    text = f"Album {album_num} of {total_albums} — {count} item(s).\n"
    if first_caption:
        preview = first_caption[:_CAPTION_PREVIEW_LEN] + (
            "..." if len(first_caption) > _CAPTION_PREVIEW_LEN else ""
        )
        text += f'Caption: "{preview}"\n'
    text += "Keep as one album post, or split into individual posts?"

    decision_row = [
        InlineKeyboardButton("Keep as album", callback_data=f"sp:keep:{placeholder_idx}"),
        InlineKeyboardButton(f"Split into {count}", callback_data=f"sp:split:{placeholder_idx}"),
    ]
    rows: list[list[InlineKeyboardButton]] = [decision_row]
    if origin_link:
        rows.append([InlineKeyboardButton("View original", url=origin_link)])
    return text, InlineKeyboardMarkup(rows)


def _apply_split_decisions(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    caption_mode: str,
    single_caption: str | None,
    single_caption_entities: list[dict[str, Any]] | None,
) -> None:
    """Rebuild bulk_posts by applying all recorded keep/split decisions."""
    posts = _get_posts(context)
    decisions: dict[int, tuple[str, list[_CollectedItem]]] = context.user_data.pop(
        "bulk_split_decisions", {}
    )
    if not decisions:
        return

    new_posts: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        if idx in decisions:
            action, items = decisions[idx]
            if action == "keep":
                new_post = dict(post)
                new_post["media_group_data"] = _finalize_media_group_items(
                    items,
                    caption_mode=caption_mode,
                    single_caption=single_caption,
                    single_caption_entities=single_caption_entities,
                )
                new_post["media_type"] = "media_group"
                new_posts.append(new_post)
            else:  # "split"
                for item in items:
                    new_posts.append(
                        {
                            "media_type": item.media_type,
                            "file_id": item.file_id,
                            "file_path": None,
                            "caption": item.caption,
                            "caption_parse_mode": None,
                            "caption_entities": json.dumps(item.caption_entities)
                            if item.caption_entities
                            else None,
                            "forward_from_chat_id": item.forward_from_chat_id,
                            "forward_from_message_id": item.forward_from_message_id,
                            "forward_origin_chat_id": item.forward_origin_chat_id,
                            "forward_origin_message_id": item.forward_origin_message_id,
                            "media_group_data": None,
                        }
                    )
        else:
            new_posts.append(post)

    context.user_data["bulk_posts"] = new_posts


async def _show_confirmation_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    via_callback: bool = False,
) -> int:
    """Send the pre-queue confirmation summary and transition to CONFIRMING."""
    posts = _get_posts(context)

    broken = [
        i
        for i, p in enumerate(posts)
        if p.get("media_type") == "media_group" and not p.get("media_group_data")
    ]
    if broken:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text=_MEDIA_GROUP_ERROR_MSG,
            )
        return ConversationHandler.END

    counts: dict[str, int] = {}
    for p in posts:
        counts[p["media_type"]] = counts.get(p["media_type"], 0) + 1

    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    schedule_id = context.user_data.get("bulk_schedule_id")

    user_id = update.effective_user.id if update.effective_user else 0
    details = await db.get_user_context_details(user_id)
    schedule_name = str(details.get("schedule_name") or f"Schedule {schedule_id}")
    segments = [
        Segment(f"Ready to queue {len(posts)} posts for schedule '{schedule_name}'.\n"),
        Segment(f"Breakdown: {', '.join(parts)}\n\n"),
        Segment("Reply 'yes' to confirm, or 'no' to cancel.\n\n"),
        *selection_segments(details),
    ]
    text, entities = render(segments)

    if via_callback and update.callback_query is not None:
        msg = update.callback_query.message
        if msg is not None:
            await context.bot.send_message(chat_id=msg.chat_id, text=text, entities=entities)
    elif update.message is not None:
        await update.message.reply_text(text, entities=entities)

    context.user_data["bulk_in_confirming"] = True
    return CONFIRMING


async def _resolve_bulk_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, int, bool, str] | None:
    """Resolve schedule id and channel; reply with error and return None on failure."""
    if update.message is None or update.effective_user is None:
        return None

    user_id = update.effective_user.id
    schedule_id: int | None = None
    used_selected = False

    if context.args and len(context.args) == 1:
        try:
            schedule_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid schedule id.")
            return None
    else:
        user_ctx = await db.get_user_context(user_id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /bulk <schedule_id>\nTip: use /select to pick a default schedule."
        )
        return None

    schedule = await db.get_schedule_for_user(user_id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return None

    return schedule_id, int(schedule["channel_id"]), used_selected, str(schedule.get("name") or "")


async def _maybe_offer_resume(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    schedule_id: int,
    channel_db_id: int,
) -> int | None:
    """Prompt to resume a pending staging session; return RESUMING if offered."""
    pending_session = await db.get_bulk_session(user_id)
    if not pending_session or int(pending_session["schedule_id"]) != schedule_id:
        return None

    pending_count = await db.get_staging_count(user_id)
    if pending_count <= 0:
        return None

    _state_clear(context)
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_channel_db_id"] = channel_db_id
    if update.message is not None:
        await update.message.reply_text(
            f"You have {pending_count} item(s) from a previous upload.\nResume or start fresh?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Resume", callback_data="bulk_resume:yes"),
                        InlineKeyboardButton("Start fresh", callback_data="bulk_resume:no"),
                    ]
                ]
            ),
        )
    return RESUMING


async def bulk_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /bulk <schedule_id>."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please run /bulk in a private chat with the bot.")
        return ConversationHandler.END

    resolved = await _resolve_bulk_schedule(update, context)
    if resolved is None:
        return ConversationHandler.END

    schedule_id, channel_db_id, used_selected, schedule_name_from_db = resolved
    user_id = update.effective_user.id

    if not used_selected:
        await db.set_user_context(
            user_id=user_id,
            selected_channel_id=channel_db_id,
            selected_schedule_id=schedule_id,
        )

    resume_state = await _maybe_offer_resume(
        update,
        context,
        user_id=user_id,
        schedule_id=schedule_id,
        channel_db_id=channel_db_id,
    )
    if resume_state is not None:
        return resume_state

    await _clear_staging(user_id)

    _state_clear(context)
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_channel_db_id"] = channel_db_id

    details = await db.get_user_context_details(user_id)
    schedule_name = str(
        details.get("schedule_name") or schedule_name_from_db or f"Schedule {schedule_id}"
    )
    segments = [
        Segment(f"Bulk upload started for schedule '{schedule_name}'.\n\n"),
        *selection_segments(details),
        Segment("\n\nChoose caption mode by replying with one of:\n"),
        Segment("- remove (strip all captions)\n"),
        Segment("- single (use one caption for all posts)\n"),
        Segment("- preserve (keep each post's original caption and formatting)\n\n"),
        Segment(
            "Tip: messages forwarded from channels in your /forwarding allowlist are always "
            "sent as native Telegram forwards, regardless of caption mode.\n"
        ),
        Segment(
            _SINGLE_CAPTION_FORMAT_TIP,
        ),
        Segment("Or /cancel to stop."),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)
    return SELECTING_CAPTION_MODE


async def _resume_staging_session(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> int:
    """Load a pending staging session and return the next conversation state."""
    session = await db.get_bulk_session(user_id)
    items = await db.get_staging_items(user_id)
    if not session or not items:
        await safe_edit_message_text(query, "Session expired. Starting fresh — run /bulk again.")
        await _clear_staging(user_id)
        _state_clear(context)
        return ConversationHandler.END

    _state_clear(context)
    _load_staging_into_user_data(context, items, session)
    schedule = await db.get_schedule(int(session["schedule_id"]))
    if schedule:
        context.user_data["bulk_channel_db_id"] = int(schedule["channel_id"])
    posts = _get_posts(context)
    await safe_edit_message_text(
        query,
        f"Resumed {len(items)} item(s) ({len(posts)} post(s)).\n"
        "Send more media, or /done to finish.",
    )
    return COLLECTING_MEDIA


async def _start_fresh_after_discard(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> int:
    """Clear staging and restart caption-mode selection."""
    await _clear_staging(user_id)
    schedule_id = context.user_data.get("bulk_schedule_id")
    _state_clear(context)
    context.user_data["bulk_schedule_id"] = schedule_id
    details = await db.get_user_context_details(user_id)
    schedule_name = str(details.get("schedule_name") or f"Schedule {schedule_id}")
    segments = [
        Segment(f"Bulk upload started for schedule '{schedule_name}'.\n\n"),
        *selection_segments(details),
        Segment("\n\nChoose caption mode by replying with one of:\n"),
        Segment("- remove (strip all captions)\n"),
        Segment("- single (use one caption for all posts)\n"),
        Segment("- preserve (keep each post's original caption and formatting)\n\n"),
        Segment("Or /cancel to stop."),
    ]
    text, entities = render(segments)
    await safe_edit_message_text(query, text, entities=entities)
    return SELECTING_CAPTION_MODE


async def bulk_resume_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the resume/discard decision for a pending staging session."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return RESUMING

    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""

    if data == "bulk_resume:yes":
        return await _resume_staging_session(query, context, user_id)

    return await _start_fresh_after_discard(query, context, user_id)


async def bulk_set_caption_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    raw = (update.message.text or "").strip().lower()
    # Backwards-compatible aliases (older prompts used these).
    if raw in {"markdown", "markdownv2", "md", "md2", "html"}:
        raw = "single"
    if raw not in {"remove", "single", "preserve"}:
        await update.message.reply_text(
            "Invalid caption mode. Reply with: remove, single, preserve"
        )
        return SELECTING_CAPTION_MODE

    context.user_data["bulk_caption_mode"] = raw

    schedule_id = context.user_data.get("bulk_schedule_id")
    if schedule_id is not None:
        await db.create_bulk_session(
            update.effective_user.id,
            schedule_id=int(schedule_id),
            caption_mode=raw,
        )

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
    if update.message is None or update.effective_user is None:
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

    await db.update_bulk_session_caption(
        update.effective_user.id,
        caption_mode="single",
        single_caption=caption_text,
        single_caption_entities=json.dumps(caption_entities) if caption_entities else None,
    )

    await update.message.reply_text(
        "Caption saved.\n"
        "Now send photos, videos, or documents.\n"
        "When you're done, send /done to queue them or /cancel to stop."
    )
    return COLLECTING_MEDIA


def _get_fingerprint_data(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    """Return the list of fingerprint dicts accumulated during the bulk session."""
    data = context.user_data.get("bulk_fingerprint_data")
    if isinstance(data, list):
        return data
    data = []
    context.user_data["bulk_fingerprint_data"] = data
    return data


async def _check_and_warn_duplicate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: _CollectedItem,
    channel_db_id: int,
) -> None:
    """Run two-layer duplicate check and send a warning with action buttons if matched."""
    user_id = update.effective_user.id if update.effective_user else 0

    def _record_fp(dhash: str | None) -> None:
        _get_fingerprint_data(context).append(
            {
                "file_unique_id": item.file_unique_id,
                "dhash": dhash,
                "file_id": item.file_id,
                "media_type": item.media_type,
            }
        )

    if not await dedup.should_check(channel_db_id=channel_db_id, user_id=user_id):
        _record_fp(None)
        return

    # Layer 1: exact file_unique_id match (all media types, zero cost).
    match_info = await dedup.find_by_file_unique_id(channel_db_id, item.file_unique_id)

    # Layer 2: perceptual dHash match (photos only, requires thumbnail download).
    if (
        match_info is None
        and item.media_type == "photo"
        and update.message
        and update.message.photo
    ):
        try:
            smallest_file_id = update.message.photo[0].file_id
            dhash_val = await compute_dhash(context.bot, smallest_file_id)
            _record_fp(str(dhash_val))
            match_info = await dedup.find_by_dhash(channel_db_id, dhash_val)
        except (TimedOut, NetworkError) as e:
            logger.info(
                "Skipping dHash for file_id=%s due to Telegram network error: %s",
                item.file_id,
                e,
            )
            _record_fp(None)
        except Exception:
            logger.exception("dHash computation failed for file_id=%s", item.file_id)
    else:
        _record_fp(None)

    if match_info is None:
        return

    posted_at = match_info.get("posted_at")
    date_str = str(posted_at)[:10] if posted_at else "an earlier upload"

    dup_seq = context.user_data.get("bulk_dup_seq", 0)
    context.user_data["bulk_dup_seq"] = dup_seq + 1
    dup_map: dict[int, str] = context.user_data.setdefault("bulk_dup_map", {})
    dup_map[dup_seq] = item.file_id

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Remove from upload", callback_data=f"dup:rm:{dup_seq}"),
                InlineKeyboardButton("Keep", callback_data=f"dup:keep:{dup_seq}"),
            ]
        ]
    )
    if update.message:
        await update.message.reply_text(
            f"This looks similar to a post from {date_str}.",
            reply_markup=keyboard,
            do_quote=True,
        )


def _parse_dup_seq(data: str) -> int | None:
    try:
        return int(data.split(":")[2])
    except IndexError, ValueError:
        return None


async def _remove_dup_from_upload(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    user_id: int,
) -> int:
    """Remove a duplicate file from in-memory state and staging; return remaining post count."""
    posts = _get_posts(context)
    context.user_data["bulk_posts"] = [p for p in posts if p.get("file_id") != file_id]

    groups = _get_media_groups(context)
    for gid, items in list(groups.items()):
        groups[gid] = [i for i in items if i.file_id != file_id]
        if not groups[gid]:
            del groups[gid]
            _get_media_group_indexes(context).pop(gid, None)

    fp_data = _get_fingerprint_data(context)
    context.user_data["bulk_fingerprint_data"] = [
        fp for fp in fp_data if fp.get("file_id") != file_id
    ]

    if user_id:
        await db.remove_staging_items_by_file_id(user_id, file_id)

    return len(_get_posts(context))


async def _handle_dup_keep(query: CallbackQuery) -> None:
    await safe_edit_message_text(query, "Kept.")


async def _handle_dup_remove(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    data: str,
) -> None:
    seq = _parse_dup_seq(data)
    if seq is None:
        return

    dup_map: dict[int, str] = context.user_data.get("bulk_dup_map", {})
    file_id = dup_map.pop(seq, None)
    if file_id is None:
        await safe_edit_message_text(query, "Already handled.")
        return

    user_id = update.effective_user.id if update.effective_user else 0
    remaining = await _remove_dup_from_upload(context, file_id=file_id, user_id=user_id)
    await safe_edit_message_text(query, f"Removed from upload. ({remaining} post(s) remaining)")


async def bulk_duplicate_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle remove/keep decisions for duplicate warnings."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    data = query.data or ""

    if data.startswith("dup:keep:"):
        await _handle_dup_keep(query)
        return

    if data.startswith("dup:rm:"):
        await _handle_dup_remove(query, context, update, data)


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

    single_caption_entities = (
        _get_single_caption_entities(context) if caption_mode == "single" else None
    )

    item = _message_to_collected_item(
        update.message,
        caption_mode=caption_mode,
        single_caption=single_caption,
        single_caption_entities=single_caption_entities,
        forward_origin_allowlist=set(
            await db.get_forward_origin_allowlist(update.effective_user.id)
        ),
    )
    if item is None:
        await update.message.reply_text(
            "Unsupported message type. Send a photo, video, or document."
        )
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
                    "caption_entities": json.dumps(single_caption_entities)
                    if single_caption_entities
                    else None,
                    "media_group_data": None,
                }
            )

        await _persist_item_to_staging(update.effective_user.id, item, media_group_id=group_id)

        channel_db_id = context.user_data.get("bulk_channel_db_id")
        if channel_db_id:
            await _check_and_warn_duplicate(update, context, item, int(channel_db_id))

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
            "caption_entities": json.dumps(item.caption_entities)
            if item.caption_entities
            else None,
            "forward_from_chat_id": item.forward_from_chat_id,
            "forward_from_message_id": item.forward_from_message_id,
            "forward_origin_chat_id": item.forward_origin_chat_id,
            "forward_origin_message_id": item.forward_origin_message_id,
            "media_group_data": None,
        }
    )

    await _persist_item_to_staging(update.effective_user.id, item)

    channel_db_id = context.user_data.get("bulk_channel_db_id")
    if channel_db_id:
        await _check_and_warn_duplicate(update, context, item, int(channel_db_id))

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
    single_caption_entities = (
        _get_single_caption_entities(context) if caption_mode == "single" else None
    )

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
                "caption_entities": json.dumps(single_caption_entities)
                if single_caption_entities
                else None,
                "media_group_data": media_group_data,
            }
        )
        return

    posts[idx]["media_type"] = "media_group"
    posts[idx]["file_id"] = None
    posts[idx]["file_path"] = None
    posts[idx]["caption"] = None
    posts[idx]["caption_parse_mode"] = None
    posts[idx]["caption_entities"] = (
        json.dumps(single_caption_entities) if single_caption_entities else None
    )
    posts[idx]["media_group_data"] = media_group_data


async def bulk_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalize collection, handle any split prompts, then ask for confirmation."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    allowlist = set(await db.get_forward_origin_allowlist(update.effective_user.id))

    # Separate groups that need a user decision from those that can be flushed directly.
    pending_splits: list[dict[str, Any]] = []
    groups_snapshot = dict(_get_media_groups(context))

    for gid, items in groups_snapshot.items():
        if _group_needs_split_prompt(items, allowlist):
            # Don't flush yet — pop from the buffer and store for the prompt loop.
            _get_media_groups(context).pop(gid, None)
            placeholder_idx = _get_media_group_indexes(context).pop(gid, None)
            if placeholder_idx is not None:
                first_item = items[0]
                pending_splits.append(
                    {
                        "placeholder_idx": placeholder_idx,
                        "items": items,
                        "count": len(items),
                        "first_caption": next((i.caption for i in items if i.caption), None),
                        "origin_link": _origin_link(
                            first_item.raw_origin_chat_id,
                            first_item.raw_origin_message_id,
                        ),
                        "reply_to_message_id": first_item.user_message_id,
                    }
                )
            else:
                # No placeholder recorded — flush as album silently.
                await _flush_media_group(context, group_id=gid)
        else:
            await _flush_media_group(context, group_id=gid)

    posts = _get_posts(context)
    if not posts:
        await update.message.reply_text("No posts collected yet. Send media, then /done.")
        return COLLECTING_MEDIA

    if pending_splits:
        total_albums = len(pending_splits)
        context.user_data["bulk_pending_splits"] = pending_splits
        context.user_data["bulk_split_decisions"] = {}
        context.user_data["bulk_total_splits"] = total_albums
        first = pending_splits[0]
        text, keyboard = _build_split_prompt(
            first["count"],
            first["placeholder_idx"],
            album_num=1,
            total_albums=total_albums,
            first_caption=first.get("first_caption"),
            origin_link=first.get("origin_link"),
        )
        reply_to = first.get("reply_to_message_id")
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=text,
            reply_markup=keyboard,
            reply_to_message_id=reply_to,
        )
        return DECIDING_SPLITS

    return await _show_confirmation_message(update, context)


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
    channel_db_id = context.user_data.get("bulk_channel_db_id")
    fp_data = _get_fingerprint_data(context)

    inserted, _post_ids = await posting.enqueue_bulk(
        schedule_id,
        posts=posts,
        fingerprints=fp_data if fp_data else None,
        channel_db_id=int(channel_db_id) if channel_db_id else None,
    )

    # 'empty_paused' is system-set when the queue ran dry; with new posts
    # its only reason is gone, so resume. 'paused' is a user choice — leave it.
    auto_resumed = schedule.get("state") == "empty_paused"
    if auto_resumed:
        await scheduling.resume(schedule_id, user_id=update.effective_user.id)

    await _clear_staging(update.effective_user.id)
    _state_clear(context)
    details = await db.get_user_context_details(update.effective_user.id)
    sched_name = str(schedule.get("name") or f"Schedule {schedule_id}")
    segments: list[Segment] = [Segment(f"Queued {inserted} posts for '{sched_name}'.\n")]
    if auto_resumed:
        segments.append(Segment("Schedule was empty and is now active.\n\n"))
    elif schedule.get("state") == "paused":
        segments.append(Segment("Use /schedules to resume posting when ready.\n\n"))
    else:
        segments.append(Segment("\n"))
    segments.extend(selection_segments(details))
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)
    logger.info(
        "User %s queued %s posts for schedule id=%s",
        update.effective_user.id,
        inserted,
        schedule_id,
    )
    return ConversationHandler.END


async def bulk_split_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle keep/split decisions for forwarded albums during /done."""
    query = update.callback_query
    if query is None:
        return DECIDING_SPLITS

    await query.answer()
    data = query.data or ""

    pending: list[dict[str, Any]] = context.user_data.get("bulk_pending_splits", [])
    if not pending:
        # Nothing left to decide — proceed to confirmation.
        caption_mode = _get_caption_mode(context) or "remove"
        _apply_split_decisions(
            context,
            caption_mode=caption_mode,
            single_caption=_get_single_caption(context),
            single_caption_entities=_get_single_caption_entities(context),
        )
        return await _show_confirmation_message(update, context, via_callback=True)

    current = pending[0]
    expected_idx = current["placeholder_idx"]

    if data.startswith("sp:keep:") or data.startswith("sp:split:"):
        parts = data.split(":")
        try:
            idx = int(parts[2])
        except IndexError, ValueError:
            return DECIDING_SPLITS

        if idx != expected_idx:
            await query.answer("This decision has already been processed.", show_alert=True)
            return DECIDING_SPLITS

        action = "keep" if data.startswith("sp:keep:") else "split"
        decisions: dict[int, Any] = context.user_data.setdefault("bulk_split_decisions", {})
        decisions[expected_idx] = (action, current["items"])
        pending.pop(0)

        if pending:
            next_item = pending[0]
            # total_albums is fixed at the start; album_num is derived from how
            # many remain vs. how many were originally queued.
            total_albums = context.user_data.get("bulk_total_splits", len(pending))
            album_num = total_albums - len(pending) + 1
            text, keyboard = _build_split_prompt(
                next_item["count"],
                next_item["placeholder_idx"],
                album_num=album_num,
                total_albums=total_albums,
                first_caption=next_item.get("first_caption"),
                origin_link=next_item.get("origin_link"),
            )
            await safe_edit_message_text(query, text, reply_markup=keyboard)
            return DECIDING_SPLITS

    # All decisions recorded — apply and show confirmation.
    caption_mode = _get_caption_mode(context) or "remove"
    _apply_split_decisions(
        context,
        caption_mode=caption_mode,
        single_caption=_get_single_caption(context),
        single_caption_entities=_get_single_caption_entities(context),
    )
    return await _show_confirmation_message(update, context, via_callback=True)


async def bulk_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.effective_user:
        await _clear_staging(update.effective_user.id)
    _state_clear(context)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


bulk_upload_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("bulk", bulk_start)],
    states={
        RESUMING: [
            CallbackQueryHandler(bulk_resume_decision, pattern=r"^bulk_resume:"),
            CommandHandler("cancel", bulk_cancel),
        ],
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
            CallbackQueryHandler(bulk_duplicate_decision, pattern=r"^dup:"),
            CommandHandler("done", bulk_done),
            CommandHandler("cancel", bulk_cancel),
        ],
        CONFIRMING: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_confirm),
            MessageHandler(
                filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
                bulk_collect_media,
            ),
            CallbackQueryHandler(bulk_duplicate_decision, pattern=r"^dup:"),
        ],
        DECIDING_SPLITS: [
            CallbackQueryHandler(bulk_split_decision, pattern=r"^sp:"),
            CommandHandler("cancel", bulk_cancel),
        ],
    },
    fallbacks=[CommandHandler("cancel", bulk_cancel)],
    name="bulk_upload",
    persistent=True,
)
