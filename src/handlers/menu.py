"""Menu system and persistent reply keyboard."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from handlers.queue_management import send_queue_browser
from scheduler.timing import calculate_next_run
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)

# Persistent reply keyboard — set once in /start and kept alive by /menu.
PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [["Menu"]],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------------------------------------------------------------------------
# Callback data tokens
# Format: mn:chs                   — show channels list
#         mn:ch:{channel_id}       — show schedules for a channel
#         mn:sc:{schedule_id}      — show schedule status card (and select it)
#         mn:vq:{schedule_id}      — open queue browser (new message)
#         mn:up:{schedule_id}      — upload hint (new message)
#         mn:pa:{schedule_id}      — pause schedule
#         mn:re:{schedule_id}      — resume schedule
#         mn:da:{schedule_id}      — delete schedule (ask)
#         mn:do:{schedule_id}      — delete schedule (confirm)
# ---------------------------------------------------------------------------
_CB_CHANNELS = "mn:chs"
_CB_CHANNEL = "mn:ch"
_CB_SCHEDULE = "mn:sc"
_CB_VIEW_QUEUE = "mn:vq"
_CB_UPLOAD = "mn:up"
_CB_PAUSE = "mn:pa"
_CB_RESUME = "mn:re"
_CB_DEL_ASK = "mn:da"
_CB_DEL_OK = "mn:do"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_dt(dt: datetime, *, tz_name: str) -> str:
    """Format a datetime as 'Mon 28 Mar 2026, 14:00' in the given timezone."""
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:
        tz = timezone.utc
    local = dt.astimezone(tz)
    return local.strftime(f"%a {local.day} %b %Y, %H:%M")


def _est_completion(schedule: dict[str, Any], count: int) -> datetime | None:
    """Estimate when the last queued post will be sent."""
    if count <= 0:
        return None
    cursor = datetime.now(timezone.utc)
    try:
        for _ in range(min(count, 1000)):
            cursor = calculate_next_run(schedule, after=cursor)
        return cursor
    except Exception:
        return None


def _parse_pattern(schedule: dict[str, Any]) -> dict[str, Any]:
    """Return the schedule pattern as a dict, parsing from JSON string if needed."""
    raw = schedule.get("pattern") or {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def _pattern_summary(pattern: dict[str, Any]) -> str:
    pt = pattern.get("type", "?")
    if pt == "interval":
        h = int(pattern.get("hours", 0) or 0)
        m = int(pattern.get("minutes", 0) or 0)
        parts = [f"{h}h" if h else "", f"{m}m" if m else ""]
        return "Every " + " ".join(p for p in parts if p)
    if pt == "daily":
        return "Daily at " + ", ".join(pattern.get("times", []))
    if pt == "weekly":
        days = ", ".join(d.capitalize() for d in pattern.get("days", []))
        times = ", ".join(pattern.get("times", []))
        return f"{days} at {times}"
    return pt


# ---------------------------------------------------------------------------
# Page builders — return (text, entities, keyboard) tuples
# ---------------------------------------------------------------------------

async def _channels_page(user_id: int) -> tuple[str, list | None, InlineKeyboardMarkup]:
    channels = await db.get_user_channels(user_id)
    if not channels:
        text, entities = render([Segment("No channels yet. Use /addchannel to add one.")])
        return text, entities or None, InlineKeyboardMarkup([])

    rows = []
    for ch in channels:
        channel_db_id = int(ch["id"])
        name = str(ch.get("channel_name") or f"Channel {channel_db_id}")
        count = await db.get_channel_queue_count(channel_db_id)
        label = f"{name} ({count} queued)"
        rows.append([InlineKeyboardButton(label, callback_data=f"{_CB_CHANNEL}:{channel_db_id}")])

    return "Your channels:", None, InlineKeyboardMarkup(rows)


async def _schedules_page(
    user_id: int,
    channel_db_id: int,
) -> tuple[str, list | None, InlineKeyboardMarkup] | None:
    channel = await db.get_channel_by_id(channel_db_id)
    if channel is None or int(channel["user_id"]) != user_id:
        return None

    channel_name = str(channel.get("channel_name") or f"Channel {channel_db_id}")
    schedules = await db.get_channel_schedules(channel_db_id)

    rows = []
    for sched in schedules:
        sched_id = int(sched["id"])
        name = str(sched.get("name") or f"Schedule {sched_id}")
        state = str(sched.get("state") or "unknown")
        count = await db.get_queue_count(sched_id)
        state_label = {"active": "active", "paused": "paused", "empty_paused": "empty"}.get(state, state)
        rows.append([InlineKeyboardButton(
            f"{name} — {state_label} ({count})",
            callback_data=f"{_CB_SCHEDULE}:{sched_id}",
        )])

    rows.append([InlineKeyboardButton("< Back", callback_data=_CB_CHANNELS)])

    if not schedules:
        text = f"{channel_name}: no schedules. Use /newschedule to create one."
    else:
        text = f"{channel_name}:"

    return text, None, InlineKeyboardMarkup(rows)


async def _schedule_card(
    user_id: int,
    schedule_id: int,
) -> tuple[str, list | None, InlineKeyboardMarkup] | None:
    schedule = await db.get_schedule_for_user(user_id, schedule_id)
    if schedule is None:
        return None

    channel_id = int(schedule["channel_id"])
    tz_name = str(schedule.get("timezone") or "UTC")
    state = str(schedule.get("state") or "unknown")
    name = str(schedule.get("name") or f"Schedule {schedule_id}")
    total = await db.get_queue_count(schedule_id)
    pattern = _parse_pattern(schedule)

    next_run_str = "n/a"
    if state == "active":
        try:
            next_run = calculate_next_run(schedule, after=datetime.now(timezone.utc))
            next_run_str = _fmt_dt(next_run, tz_name=tz_name)
        except Exception:
            next_run_str = "error"

    completion = _est_completion(schedule, total)
    completion_str = _fmt_dt(completion, tz_name=tz_name) if completion else "n/a"

    state_pretty = {
        "active": "Active",
        "paused": "Paused",
        "empty_paused": "Paused (empty queue)",
    }.get(state, state)

    segments: list[Segment] = [
        Segment(name, code=True),
        Segment(f"\nState: {state_pretty} | Queue: {total} post(s)\n"),
        Segment(f"Pattern: {_pattern_summary(pattern)}\n"),
        Segment(f"Timezone: {tz_name}\n"),
        Segment(f"Next run: {next_run_str}\n"),
        Segment(f"Est. completion: {completion_str}"),
    ]
    text, entities = render(segments)

    if state in ("active", "empty_paused"):
        toggle_btn = InlineKeyboardButton("Pause", callback_data=f"{_CB_PAUSE}:{schedule_id}")
    else:
        toggle_btn = InlineKeyboardButton("Resume", callback_data=f"{_CB_RESUME}:{schedule_id}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("View Queue", callback_data=f"{_CB_VIEW_QUEUE}:{schedule_id}"),
            InlineKeyboardButton("Upload", callback_data=f"{_CB_UPLOAD}:{schedule_id}"),
        ],
        [
            toggle_btn,
            InlineKeyboardButton("Delete", callback_data=f"{_CB_DEL_ASK}:{schedule_id}"),
        ],
        [InlineKeyboardButton("< Back", callback_data=f"{_CB_CHANNEL}:{channel_id}")],
    ])

    return text, entities or None, keyboard


# ---------------------------------------------------------------------------
# Command and callback handlers
# ---------------------------------------------------------------------------

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main channel menu as an inline keyboard."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    text, entities, keyboard = await _channels_page(update.effective_user.id)
    await update.message.reply_text(text, entities=entities, reply_markup=keyboard)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all mn:* inline keyboard callbacks."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id
    parts = data.split(":")

    # -----------------------------------------------------------------------
    # mn:chs — channels list
    # -----------------------------------------------------------------------
    if data == _CB_CHANNELS:
        text, entities, keyboard = await _channels_page(user_id)
        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:ch:{channel_id} — schedules for a channel
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_CHANNEL}:"):
        try:
            channel_id = int(parts[2])
        except (IndexError, ValueError):
            await query.edit_message_text("Invalid data.")
            return

        result = await _schedules_page(user_id, channel_id)
        if result is None:
            await query.edit_message_text("Channel not found or not owned by you.")
            return

        text, entities, keyboard = result
        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:sc:{schedule_id} — schedule status card (auto-selects it)
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_SCHEDULE}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            await query.edit_message_text("Invalid data.")
            return

        schedule = await db.get_schedule_for_user(user_id, schedule_id)
        if schedule is not None:
            await db.set_user_context(
                user_id=user_id,
                selected_channel_id=int(schedule["channel_id"]),
                selected_schedule_id=schedule_id,
            )

        result = await _schedule_card(user_id, schedule_id)
        if result is None:
            await query.edit_message_text("Schedule not found or not owned by you.")
            return

        text, entities, keyboard = result
        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:vq:{schedule_id} — open queue browser as a new message
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_VIEW_QUEUE}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        msg = query.message
        if msg is not None:
            await send_queue_browser(
                user_id=user_id,
                schedule_id=schedule_id,
                chat_id=msg.chat_id,
                bot=context.bot,
            )
        return

    # -----------------------------------------------------------------------
    # mn:up:{schedule_id} — select schedule and send upload guidance
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_UPLOAD}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        schedule = await db.get_schedule_for_user(user_id, schedule_id)
        if schedule is not None:
            await db.set_user_context(
                user_id=user_id,
                selected_channel_id=int(schedule["channel_id"]),
                selected_schedule_id=schedule_id,
            )

        msg = query.message
        if msg is not None:
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text="Schedule selected. Use /bulk to start uploading posts.",
            )
        return

    # -----------------------------------------------------------------------
    # mn:pa:{schedule_id} — pause
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_PAUSE}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        if await db.get_schedule_for_user(user_id, schedule_id) is None:
            await query.edit_message_text("Schedule not found or not owned by you.")
            return

        await db.update_schedule_state(schedule_id, "paused")

        result = await _schedule_card(user_id, schedule_id)
        if result is None:
            return
        text, entities, keyboard = result
        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:re:{schedule_id} — resume (blocked if queue is empty)
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_RESUME}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        if await db.get_schedule_for_user(user_id, schedule_id) is None:
            await query.edit_message_text("Schedule not found or not owned by you.")
            return

        if await db.get_queue_count(schedule_id) == 0:
            channel_id_raw = (await db.get_schedule_for_user(user_id, schedule_id) or {}).get("channel_id")
            back_cb = f"{_CB_SCHEDULE}:{schedule_id}"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("< Back", callback_data=back_cb)]])
            try:
                await query.edit_message_text(
                    "The queue is empty. Upload posts with /bulk before resuming.",
                    reply_markup=keyboard,
                )
            except Exception:
                pass
            return

        await db.update_schedule_state(schedule_id, "active")

        result = await _schedule_card(user_id, schedule_id)
        if result is None:
            return
        text, entities, keyboard = result
        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:da:{schedule_id} — delete schedule (ask)
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_DEL_ASK}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        schedule = await db.get_schedule_for_user(user_id, schedule_id)
        if schedule is None:
            await query.edit_message_text("Schedule not found or not owned by you.")
            return

        name = str(schedule.get("name") or f"Schedule {schedule_id}")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm delete", callback_data=f"{_CB_DEL_OK}:{schedule_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"{_CB_SCHEDULE}:{schedule_id}"),
        ]])
        try:
            await query.edit_message_text(
                f"Delete schedule \"{name}\"?\n"
                "All queued posts will also be deleted. This cannot be undone.",
                reply_markup=keyboard,
            )
        except Exception:
            pass
        return

    # -----------------------------------------------------------------------
    # mn:do:{schedule_id} — delete schedule (confirmed)
    # -----------------------------------------------------------------------
    if data.startswith(f"{_CB_DEL_OK}:"):
        try:
            schedule_id = int(parts[2])
        except (IndexError, ValueError):
            return

        schedule = await db.get_schedule_for_user(user_id, schedule_id)
        if schedule is None:
            await query.edit_message_text("Schedule not found or not owned by you.")
            return

        channel_id = int(schedule["channel_id"])
        await db.delete_schedule(schedule_id)

        result = await _schedules_page(user_id, channel_id)
        if result is None:
            text = "Schedule deleted."
            entities = None
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("< Back", callback_data=_CB_CHANNELS)]])
        else:
            text, entities, keyboard = result

        try:
            await query.edit_message_text(text, entities=entities, reply_markup=keyboard)
        except Exception:
            pass
        return

    logger.warning("Unhandled menu callback data: %r", data)
