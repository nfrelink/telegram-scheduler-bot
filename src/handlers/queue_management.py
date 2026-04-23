"""Queue inspection and management commands."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import re
from datetime import date as _date

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import queries as db
from database.time import parse_timestamp
from handlers.common import ensure_user_record, parse_int
from scheduler.timing import calculate_next_run
from services import posting
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


def _format_dt(dt: datetime, *, tz_name: str | None = None) -> str:
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    return dt.astimezone(tz).replace(microsecond=0).isoformat()


def _media_group_is_forwarded(media_group_data: object) -> bool:
    if not isinstance(media_group_data, str) or not media_group_data.strip():
        return False
    try:
        items = json.loads(media_group_data)
    except Exception:
        return False
    if not isinstance(items, list) or not items:
        return False
    first = items[0]
    if not isinstance(first, dict):
        return False
    return (
        first.get("forward_from_chat_id") is not None
        and first.get("forward_from_message_id") is not None
    )


# ---------------------------------------------------------------------------
# Queue browser — callback data tokens
# Format: qv:go:{schedule_id}:{offset}
#         qv:da:{post_id}:{schedule_id}:{offset}   (delete ask)
#         qv:do:{post_id}:{schedule_id}:{offset}   (delete confirmed)
#         qv:al:{post_id}                           (show full album)
#         qv:pd:{post_id}                           (set pinned date — ConversationHandler entry)
#         qv:cp:{post_id}:{schedule_id}:{offset}   (clear pinned date)
#         qv:noop                                    (non-interactive position button)
# ---------------------------------------------------------------------------
_CB_GO = "qv:go"
_CB_DEL_ASK = "qv:da"
_CB_DEL_OK = "qv:do"
_CB_ALBUM = "qv:al"
_CB_PIN_DATE = "qv:pd"  # entry point for pin-date conversation
_CB_CLEAR_PIN = (
    "qv:cp"  # clear pinned_at — format: qv:cp:{post_id}:{schedule_id}:{offset}
)
_CB_NOOP = "qv:noop"


@dataclass
class _QueuePage:
    text: str
    entities: list | None
    keyboard: InlineKeyboardMarkup
    file_id: str | None = None
    file_media_type: str | None = None  # 'photo', 'video', 'document'
    is_empty: bool = False


def _first_media_from_group(post: dict[str, Any]) -> tuple[str, str] | None:
    """Return (file_id, media_type) for the first displayable item in a media group."""
    media_group_data = post.get("media_group_data")
    if not media_group_data:
        return None
    try:
        items = json.loads(media_group_data)
        if items and isinstance(items[0], dict):
            fid = items[0].get("file_id")
            mt = items[0].get("media_type")
            if fid and mt in ("photo", "video", "document"):
                return str(fid), str(mt)
    except Exception:
        pass
    return None


def _all_media_from_group(
    post: dict[str, Any],
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """Return InputMedia objects for every item in a media_group post that has a file_id."""
    media_group_data = post.get("media_group_data")
    if not media_group_data:
        return []
    try:
        items = json.loads(media_group_data)
    except Exception:
        return []
    result: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = item.get("file_id")
        mt = item.get("media_type")
        if not fid or mt not in ("photo", "video", "document"):
            continue
        cap = (item.get("caption") or "").strip() or None
        cap_entities_raw = item.get("caption_entities")
        cap_entities: list | None = None
        if cap_entities_raw:
            try:
                cap_entities = (
                    json.loads(cap_entities_raw)
                    if isinstance(cap_entities_raw, str)
                    else cap_entities_raw
                )
            except Exception:
                cap_entities = None
        result.append(
            _to_input_media(
                file_id=str(fid),
                media_type=str(mt),
                caption=cap,
                caption_entities=cap_entities,
            )
        )
    return result


def _to_input_media(
    *,
    file_id: str,
    media_type: str,
    caption: str | None,
    caption_entities: list | None,
) -> InputMediaPhoto | InputMediaVideo | InputMediaDocument:
    match media_type:
        case "photo":
            return InputMediaPhoto(
                media=file_id, caption=caption, caption_entities=caption_entities
            )
        case "video":
            return InputMediaVideo(
                media=file_id, caption=caption, caption_entities=caption_entities
            )
        case _:
            return InputMediaDocument(
                media=file_id, caption=caption, caption_entities=caption_entities
            )


def _get_display_caption(post: dict[str, Any]) -> str | None:
    """Return the caption text for a post, checking media_group_data if needed."""
    caption = (post.get("caption") or "").strip()
    if caption:
        return caption
    media_group_data = post.get("media_group_data")
    if media_group_data:
        try:
            items = json.loads(media_group_data)
            if items and isinstance(items[0], dict):
                return (items[0].get("caption") or "").strip() or None
        except Exception:
            pass
    return None


def _is_native_forward(post: dict[str, Any]) -> bool:
    return post.get("forward_from_chat_id") is not None or _media_group_is_forwarded(
        post.get("media_group_data")
    )


def _format_dt_browser(dt: datetime, *, tz_name: str | None = None) -> str:
    """Format a datetime as 'Mon 28 Mar 2026, 14:00' in the given timezone."""
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    local = dt.astimezone(tz)
    return local.strftime(f"%a {local.day} %b %Y, %H:%M")


def _estimate_send_time(schedule: dict[str, Any], offset: int) -> datetime | None:
    """Estimate when the post at the given queue offset will be sent."""
    cursor = datetime.now(timezone.utc)
    try:
        for _ in range(offset + 1):
            cursor = calculate_next_run(schedule, after=cursor)
        return cursor
    except Exception:
        return None


def _estimate_completion(schedule: dict[str, Any], count: int) -> datetime | None:
    """Estimate when the last post in a queue of `count` posts will be sent."""
    if count <= 0:
        return None
    cursor = datetime.now(timezone.utc)
    try:
        for _ in range(min(count, 1000)):
            cursor = calculate_next_run(schedule, after=cursor)
        return cursor
    except Exception:
        return None


def _queue_nav_keyboard(
    *,
    schedule_id: int,
    offset: int,
    total: int,
    post_id: int,
    album_count: int = 0,
    pinned_at: datetime | None = None,
) -> InlineKeyboardMarkup:
    prev_btn = (
        InlineKeyboardButton(
            "< Prev", callback_data=f"{_CB_GO}:{schedule_id}:{offset - 1}"
        )
        if offset > 0
        else InlineKeyboardButton("-", callback_data=_CB_NOOP)
    )
    pos_btn = InlineKeyboardButton(f"{offset + 1} / {total}", callback_data=_CB_NOOP)
    next_btn = (
        InlineKeyboardButton(
            "Next >", callback_data=f"{_CB_GO}:{schedule_id}:{offset + 1}"
        )
        if offset < total - 1
        else InlineKeyboardButton("-", callback_data=_CB_NOOP)
    )
    delete_btn = InlineKeyboardButton(
        "Delete this post",
        callback_data=f"{_CB_DEL_ASK}:{post_id}:{schedule_id}:{offset}",
    )
    rows: list[list[InlineKeyboardButton]] = [[prev_btn, pos_btn, next_btn]]
    if album_count > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Show album ({album_count} items)",
                    callback_data=f"{_CB_ALBUM}:{post_id}",
                )
            ]
        )
    if pinned_at is not None:
        date_label = f"{pinned_at.day} {pinned_at.strftime('%b')}"
        rows.append(
            [
                InlineKeyboardButton(
                    f"Clear date ({date_label})",
                    callback_data=f"{_CB_CLEAR_PIN}:{post_id}:{schedule_id}:{offset}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "Set date",
                    callback_data=f"{_CB_PIN_DATE}:{post_id}",
                )
            ]
        )
    rows.append([delete_btn])
    return InlineKeyboardMarkup(rows)


def _queue_confirm_keyboard(
    *,
    post_id: int,
    schedule_id: int,
    offset: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm delete",
                    callback_data=f"{_CB_DEL_OK}:{post_id}:{schedule_id}:{offset}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"{_CB_GO}:{schedule_id}:{offset}",
                ),
            ]
        ]
    )


async def _build_queue_page(
    *,
    user_id: int,
    schedule_id: int,
    offset: int,
) -> _QueuePage | None:
    """Build one page of the queue browser.

    Returns a _QueuePage or None if the schedule is not found / not owned.
    Offset is clamped to the valid range automatically.
    """
    schedule = await db.get_schedule_for_user(user_id, schedule_id)
    if schedule is None:
        return None

    schedule_tz = str(schedule.get("timezone") or "UTC")
    user_tz = await db.get_user_timezone(user_id)
    tz_name = user_tz or schedule_tz
    total = await db.get_queue_count(schedule_id)

    if total == 0:
        schedule_name = str(schedule.get("name") or f"Schedule {schedule_id}")
        segments = [
            Segment(f"Queue: {schedule_name}\nQueue is empty."),
        ]
        text, entities = render(segments)
        return _QueuePage(
            text=text,
            entities=entities or None,
            keyboard=InlineKeyboardMarkup([]),
            is_empty=True,
        )

    offset = max(0, min(offset, total - 1))

    posts = await db.get_queued_posts(schedule_id, limit=1, offset=offset)
    if not posts:
        return None

    post = posts[0]
    post_id = int(post["id"])
    media_type = str(post.get("media_type") or "unknown")
    retry_count = int(post.get("retry_count") or 0)
    is_forward = _is_native_forward(post)
    caption = _get_display_caption(post)

    # Resolve which file to display and what Telegram media type it is.
    display_file_id: str | None = None
    display_media_type: str | None = None
    if media_type in ("photo", "video", "document"):
        raw_fid = post.get("file_id")
        if raw_fid:
            display_file_id = str(raw_fid)
            display_media_type = media_type
    elif media_type == "media_group":
        group_result = _first_media_from_group(post)
        if group_result:
            display_file_id, display_media_type = group_result

    pinned_at = parse_timestamp(post.get("pinned_at"))
    scheduled_for = parse_timestamp(post.get("scheduled_for"))
    est_send = scheduled_for or _estimate_send_time(schedule, offset)
    completion = _estimate_completion(schedule, total)

    schedule_name = str(schedule.get("name") or f"Schedule {schedule_id}")
    state = str(schedule.get("state") or "unknown")

    segments: list[Segment] = [
        Segment(f"Queue: {schedule_name}\nState: {state} | Total: {total} post(s)\n"),
    ]
    if completion:
        segments.append(
            Segment(
                f"Est. completion: {_format_dt_browser(completion, tz_name=tz_name)} ({tz_name})\n"
            )
        )

    segments.append(Segment(f"\nPost {offset + 1} of {total}\n"))

    album_count = 0
    if media_type == "media_group":
        try:
            album_count = len(json.loads(post.get("media_group_data") or "[]"))
        except Exception:
            album_count = 0
        type_label = f"album ({album_count} items)" + (
            " — native forward" if is_forward else ""
        )
    else:
        type_label = media_type + (" — native forward" if is_forward else "")
    segments.append(Segment(f"Type: {type_label}\n"))

    if not is_forward:
        if caption:
            preview = caption[:60] + ("..." if len(caption) > 60 else "")
            segments.append(Segment(f'Caption: "{preview}"\n'))
        else:
            segments.append(Segment("Caption: none\n"))

    if retry_count:
        segments.append(Segment(f"Retries: {retry_count}\n"))

    if pinned_at is not None:
        segments.append(
            Segment(
                f"Pinned to: {_format_dt_browser(pinned_at, tz_name=tz_name)} ({tz_name})"
            )
        )
    elif est_send:
        segments.append(
            Segment(
                f"Est. send: {_format_dt_browser(est_send, tz_name=tz_name)} ({tz_name})"
            )
        )

    text, entities = render(segments)
    keyboard = _queue_nav_keyboard(
        schedule_id=schedule_id,
        offset=offset,
        total=total,
        post_id=post_id,
        album_count=album_count,
        pinned_at=pinned_at,
    )
    return _QueuePage(
        text=text,
        entities=entities or None,
        keyboard=keyboard,
        file_id=display_file_id,
        file_media_type=display_media_type,
    )


async def queue_browser_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle all qv:* inline keyboard callbacks for the queue browser."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    if data == _CB_NOOP:
        return

    parts = data.split(":")

    if data.startswith(f"{_CB_GO}:"):
        try:
            schedule_id = int(parts[2])
            offset = int(parts[3])
        except (IndexError, ValueError):
            await query.edit_message_caption("Navigation error.")
            return

        page = await _build_queue_page(
            user_id=user_id, schedule_id=schedule_id, offset=offset
        )
        if page is None:
            await query.edit_message_caption("Schedule not found or not owned by you.")
            return

        if page.file_id and page.file_media_type:
            input_media = _to_input_media(
                file_id=page.file_id,
                media_type=page.file_media_type,
                caption=page.text,
                caption_entities=page.entities,
            )
            try:
                await query.edit_message_media(input_media, reply_markup=page.keyboard)
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text(
                    page.text, entities=page.entities, reply_markup=page.keyboard
                )
            except Exception:
                pass
        return

    if data.startswith(f"{_CB_DEL_ASK}:"):
        try:
            post_id = int(parts[2])
            schedule_id = int(parts[3])
            offset = int(parts[4])
        except (IndexError, ValueError):
            await query.edit_message_caption("Invalid data.")
            return

        post = await db.get_queued_post_with_owner(post_id)
        if post is None or int(post["owner_user_id"]) != user_id:
            await query.edit_message_caption("Post not found or not owned by you.")
            return

        keyboard = _queue_confirm_keyboard(
            post_id=post_id, schedule_id=schedule_id, offset=offset
        )
        try:
            # Keep the media visible so the user can see what they are about to delete.
            await query.edit_message_caption(
                f"Delete post {post_id} ({post.get('media_type')})?\nThis cannot be undone.",
                reply_markup=keyboard,
            )
        except Exception:
            pass
        return

    if data.startswith(f"{_CB_DEL_OK}:"):
        try:
            post_id = int(parts[2])
            schedule_id = int(parts[3])
            offset = int(parts[4])
        except (IndexError, ValueError):
            await query.edit_message_caption("Invalid data.")
            return

        post = await db.get_queued_post_with_owner(post_id)
        if post is None or int(post["owner_user_id"]) != user_id:
            await query.edit_message_caption("Post not found or not owned by you.")
            return

        await posting.cancel(post_id=post_id, user_id=user_id)

        # Stay at the same offset; _build_queue_page clamps it to the new total.
        page = await _build_queue_page(
            user_id=user_id, schedule_id=schedule_id, offset=offset
        )
        if page is None:
            await query.edit_message_caption("Schedule not found.")
            return

        if page.is_empty:
            # Clear the old media message's keyboard, then send a new text message.
            try:
                await query.edit_message_caption(
                    "Post deleted.", reply_markup=InlineKeyboardMarkup([])
                )
            except Exception:
                pass
            msg = query.message
            if msg is not None:
                await context.bot.send_message(
                    chat_id=msg.chat_id, text="The queue is now empty."
                )
        else:
            if page.file_id and page.file_media_type:
                input_media = _to_input_media(
                    file_id=page.file_id,
                    media_type=page.file_media_type,
                    caption=page.text,
                    caption_entities=page.entities,
                )
                try:
                    await query.edit_message_media(
                        input_media, reply_markup=page.keyboard
                    )
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_text(
                        page.text, entities=page.entities, reply_markup=page.keyboard
                    )
                except Exception:
                    pass
        return

    if data.startswith(f"{_CB_CLEAR_PIN}:"):
        try:
            post_id = int(parts[2])
            schedule_id = int(parts[3])
            offset = int(parts[4])
        except (IndexError, ValueError):
            await query.edit_message_caption("Invalid data.")
            return

        post = await db.get_queued_post_with_owner(post_id)
        if post is None or int(post["owner_user_id"]) != user_id:
            await query.edit_message_caption("Post not found or not owned by you.")
            return

        await posting.unpin(post_id, user_id=user_id)

        page = await _build_queue_page(
            user_id=user_id, schedule_id=schedule_id, offset=offset
        )
        if page is None:
            await query.edit_message_caption("Schedule not found.")
            return

        if page.file_id and page.file_media_type:
            input_media = _to_input_media(
                file_id=page.file_id,
                media_type=page.file_media_type,
                caption=page.text,
                caption_entities=page.entities,
            )
            try:
                await query.edit_message_media(input_media, reply_markup=page.keyboard)
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text(
                    page.text, entities=page.entities, reply_markup=page.keyboard
                )
            except Exception:
                pass
        return

    if data.startswith(f"{_CB_ALBUM}:"):
        try:
            post_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Invalid data.", show_alert=True)
            return

        post = await db.get_queued_post_with_owner(post_id)
        if post is None or int(post["owner_user_id"]) != user_id:
            await query.answer("Post not found or not owned by you.", show_alert=True)
            return

        media_items = _all_media_from_group(post)
        if not media_items:
            await query.answer(
                "No displayable media found in this album.", show_alert=True
            )
            return

        msg = query.message
        if msg is None:
            return

        # Telegram allows 2–10 items per send_media_group.
        chunk = media_items[:10]
        truncated = len(media_items) > 10
        try:
            await context.bot.send_media_group(chat_id=msg.chat_id, media=chunk)
            if truncated:
                await context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=f"Album has {len(media_items)} items; showing first 10.",
                )
        except Exception as exc:
            logger.error(
                "Failed to send album preview for post %s: %s",
                post_id,
                exc,
                exc_info=True,
            )
            await query.answer(
                "Could not send album. Some items may be unavailable.", show_alert=True
            )
        return

    logger.warning("Unhandled queue browser callback data: %r", data)


async def view_queue_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """View the queue for a schedule as a navigable inline browser."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args:
        schedule_id = parse_int(context.args[0])
        if schedule_id is None:
            await update.message.reply_text("Invalid schedule id.")
            return
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /viewqueue [schedule_id]\n"
            "Tip: use /select to pick a default schedule."
        )
        return

    schedule = await db.get_schedule_for_user(update.effective_user.id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return

    if not used_selected:
        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(schedule["channel_id"]),
            selected_schedule_id=schedule_id,
        )

    page = await _build_queue_page(
        user_id=update.effective_user.id,
        schedule_id=schedule_id,
        offset=0,
    )
    if page is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return

    if page.file_id and page.file_media_type:
        match page.file_media_type:
            case "photo":
                await update.message.reply_photo(
                    page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
            case "video":
                await update.message.reply_video(
                    page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
            case _:
                await update.message.reply_document(
                    page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
    else:
        await update.message.reply_text(
            page.text, entities=page.entities, reply_markup=page.keyboard
        )


async def delete_post_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete a queued post by id."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /deletepost <post_id>")
        return

    post_id = parse_int(context.args[0])
    if post_id is None:
        await update.message.reply_text("Invalid post id.")
        return

    post = await db.get_queued_post_with_owner(post_id)
    if post is None or int(post["owner_user_id"]) != update.effective_user.id:
        await update.message.reply_text("Post not found or not owned by you.")
        return

    await posting.cancel(post_id=post_id, user_id=update.effective_user.id)
    text, entities = render(
        [Segment("Post "), Segment(str(post_id), code=True), Segment(" deleted.")]
    )
    await update.message.reply_text(text, entities=entities)


async def send_queue_browser(
    *,
    user_id: int,
    schedule_id: int,
    chat_id: int,
    bot: Any,
) -> None:
    """Send a fresh queue browser message for a schedule to the given chat.

    Used by the menu system to open the queue browser without editing the menu message.
    """
    page = await _build_queue_page(user_id=user_id, schedule_id=schedule_id, offset=0)
    if page is None:
        await bot.send_message(
            chat_id=chat_id, text="Schedule not found or not owned by you."
        )
        return

    if page.file_id and page.file_media_type:
        match page.file_media_type:
            case "photo":
                await bot.send_photo(
                    chat_id,
                    photo=page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
            case "video":
                await bot.send_video(
                    chat_id,
                    video=page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
            case _:
                await bot.send_document(
                    chat_id,
                    document=page.file_id,
                    caption=page.text,
                    caption_entities=page.entities,
                    reply_markup=page.keyboard,
                )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=page.text,
            entities=page.entities,
            reply_markup=page.keyboard,
        )


# ---------------------------------------------------------------------------
# Pin-date conversation
# ---------------------------------------------------------------------------

_PIN_WAITING_DATE = 0
_PIN_WAITING_TIME = 1


def _parse_date_input(text: str, *, now: _date) -> _date | None:
    """Parse a date string from the user. Returns None if unparseable.

    Accepted formats: DD/MM/YYYY, DD/MM, DD Mon YYYY, DD Mon (e.g. 25 Dec 2026).
    When year is omitted, the nearest future occurrence is chosen.
    """
    text = text.strip()

    # DD/MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # DD/MM — infer year
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        for year in (now.year, now.year + 1):
            try:
                d = _date(year, month, day)
                if d >= now:
                    return d
            except ValueError:
                continue
        return None

    # DD Mon YYYY or DD Mon
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ("%d %b", "%d %B"):
        try:
            partial = datetime.strptime(text + " 2000", fmt + " %Y").date()
        except ValueError:
            continue
        for year in (now.year, now.year + 1):
            d = partial.replace(year=year)
            if d >= now:
                return d

    return None


def _parse_time_input(text: str) -> tuple[int, int] | None:
    """Parse HH:MM (or HHMM) from user input. Returns (hour, minute) or None."""
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    m = re.fullmatch(r"(\d{2})(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    return None


async def pin_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: user taps 'Set date' in the queue browser."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return ConversationHandler.END

    await query.answer()
    data = query.data or ""
    try:
        post_id = int(data.split(":")[2])
    except (IndexError, ValueError):
        return ConversationHandler.END

    post = await db.get_queued_post_with_owner(post_id)
    if post is None or int(post["owner_user_id"]) != update.effective_user.id:
        await query.answer("Post not found or not owned by you.", show_alert=True)
        return ConversationHandler.END

    user_tz = await db.get_user_timezone(update.effective_user.id) or "UTC"
    context.user_data["pin_post_id"] = post_id
    context.user_data["pin_user_tz"] = user_tz

    msg = query.message
    if msg is None:
        return ConversationHandler.END

    await context.bot.send_message(
        chat_id=msg.chat_id,
        text=(
            f"Enter the date for this post (your timezone: {user_tz}):\n"
            "Formats: 25/12/2026 or 25/12 or 25 Dec 2026 or 25 Dec\n"
            "/cancel to abort."
        ),
    )
    return _PIN_WAITING_DATE


async def pin_date_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent a date string."""
    if update.message is None or update.effective_user is None:
        return _PIN_WAITING_DATE

    text = (update.message.text or "").strip()
    user_tz_name: str = context.user_data.get("pin_user_tz", "UTC")
    try:
        tz = ZoneInfo(user_tz_name)
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz).date()
    parsed = _parse_date_input(text, now=now_local)

    if parsed is None:
        await update.message.reply_text(
            "Could not parse that date.\n"
            "Try: 25/12/2026 or 25 Dec 2026\n"
            "/cancel to abort."
        )
        return _PIN_WAITING_DATE

    context.user_data["pin_date"] = parsed
    await update.message.reply_text(
        f"Date: {parsed.strftime('%d %b %Y')}\n"
        f"Now enter the time (HH:MM in {user_tz_name}):\n"
        "/cancel to abort."
    )
    return _PIN_WAITING_TIME


async def pin_date_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent a time string. Confirm and write pinned_at."""
    if update.message is None or update.effective_user is None:
        return _PIN_WAITING_TIME

    text = (update.message.text or "").strip()
    parsed_time = _parse_time_input(text)

    if parsed_time is None:
        await update.message.reply_text(
            "Could not parse that time. Use HH:MM (e.g. 20:00)\n/cancel to abort."
        )
        return _PIN_WAITING_TIME

    hour, minute = parsed_time
    pending_date: _date | None = context.user_data.get("pin_date")
    post_id: int | None = context.user_data.get("pin_post_id")
    user_tz_name: str = context.user_data.get("pin_user_tz", "UTC")

    if pending_date is None or post_id is None:
        await update.message.reply_text("Session expired. Please tap 'Set date' again.")
        return ConversationHandler.END

    try:
        tz = ZoneInfo(user_tz_name)
    except Exception:
        tz = timezone.utc

    local_dt = datetime(
        pending_date.year, pending_date.month, pending_date.day, hour, minute, tzinfo=tz
    )
    utc_dt = local_dt.astimezone(timezone.utc)

    if utc_dt <= datetime.now(timezone.utc):
        await update.message.reply_text(
            f"{pending_date.strftime('%d %b %Y')} {hour:02d}:{minute:02d} ({user_tz_name}) is in the past.\n"
            "Enter the date again:\n"
            "/cancel to abort."
        )
        context.user_data.pop("pin_date", None)
        return _PIN_WAITING_DATE

    await posting.pin(post_id, pinned_at=utc_dt, user_id=update.effective_user.id)
    context.user_data.pop("pin_post_id", None)
    context.user_data.pop("pin_date", None)
    context.user_data.pop("pin_user_tz", None)

    await update.message.reply_text(
        f"Post pinned to {pending_date.strftime('%d %b %Y')} at {hour:02d}:{minute:02d} ({user_tz_name}).\n"
        "It will be sent at or shortly after that time, ahead of the regular queue.\n"
        "Tap 'Clear date' in the queue browser to unpin."
    )
    return ConversationHandler.END


async def pin_date_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled the pin-date flow."""
    context.user_data.pop("pin_post_id", None)
    context.user_data.pop("pin_date", None)
    context.user_data.pop("pin_user_tz", None)
    if update.message is not None:
        await update.message.reply_text("Date pinning cancelled.")
    return ConversationHandler.END


pin_date_conversation_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(pin_date_start, pattern=r"^qv:pd:")],
    states={
        _PIN_WAITING_DATE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, pin_date_got_date)
        ],
        _PIN_WAITING_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, pin_date_got_time)
        ],
    },
    fallbacks=[CommandHandler("cancel", pin_date_cancel)],
    per_message=False,
    name="pin_date",
    persistent=True,
)


def _unused_for_type_checking(_: Any) -> None:
    # Avoid unused import warnings when type checkers are enabled.
    return
