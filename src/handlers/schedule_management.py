"""Schedule management — single /schedules command.

/schedules lists schedules for the currently selected channel.  Each schedule
has inline action buttons (Pause/Resume, Edit, Set TZ, Delete).  Delete shows
cascade counts and asks for inline confirmation.  New and Edit transition into
embedded wizard states so everything stays in one conversation.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import queries as db
from handlers.common import ensure_user_record, parse_int
from scheduler.timing import WEEKDAY_NAME_TO_INT, parse_time_string, validate_schedule_pattern
from services import scheduling
from utils.tz import default_timezone_name, is_valid_timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants — unique across the merged ConversationHandler
# ---------------------------------------------------------------------------

SM_SHOWING = 0        # schedule list with action buttons
SM_WAIT_TZ_INPUT = 1  # awaiting timezone string for set-timezone action

# Embedded new-schedule wizard states
NS_WAIT_NAME = 10
NS_WAIT_TYPE = 11
NS_WAIT_INTERVAL = 12
NS_WAIT_DAILY_TIMES = 13
NS_WAIT_WEEKLY_DAYS = 14
NS_WAIT_WEEKLY_TIMES = 15

# Embedded edit-schedule wizard states
ES_WAIT_FIELD = 20
ES_WAIT_NAME = 21
ES_WAIT_TYPE = 22
ES_WAIT_INTERVAL = 23
ES_WAIT_DAILY_TIMES = 24
ES_WAIT_WEEKLY_DAYS = 25
ES_WAIT_WEEKLY_TIMES = 26


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _effective_user_timezone_name(user_id: int) -> str:
    tz = await db.get_user_timezone(user_id)
    return tz or default_timezone_name()


def _parse_schedule_id(text: str) -> int | None:
    return parse_int(text)


def _parse_interval_input(text: str) -> tuple[int, int] | None:
    """Parse an interval like '1h', '30m', or '90' (minutes)."""
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return None
    if raw.endswith("h"):
        n = parse_int(raw[:-1])
        return (n, 0) if n and n > 0 else None
    if raw.endswith("m"):
        n = parse_int(raw[:-1])
        return (0, n) if n and n > 0 else None
    n = parse_int(raw)
    return (0, n) if n and n > 0 else None


def _parse_times_csv(text: str) -> list[str] | None:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts or not all(parse_time_string(p) for p in parts):
        return None
    normalized = []
    for p in parts:
        h, m = parse_time_string(p) or (0, 0)
        normalized.append(f"{h:02d}:{m:02d}")
    return normalized


def _parse_weekdays_csv(text: str) -> list[str] | None:
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    if not parts or not all(p in WEEKDAY_NAME_TO_INT for p in parts):
        return None
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _pattern_summary(pattern: dict, *, tz_name: str | None = None) -> str:
    tz_label = tz_name or "UTC"
    t = pattern.get("type")
    if t == "interval":
        h = int(pattern.get("hours", 0) or 0)
        m = int(pattern.get("minutes", 0) or 0)
        return f"interval ({h}h {m}m)"
    if t == "daily":
        return f"daily ({', '.join(pattern.get('times', []))} {tz_label})"
    if t == "weekly":
        days = ", ".join(pattern.get("days", []))
        times = ", ".join(pattern.get("times", []))
        return f"weekly ({days} at {times} {tz_label})"
    return "unknown"


def _clear_ns_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("ns_"):
            context.user_data.pop(key, None)


def _clear_es_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("es_"):
            context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# /schedules — list display
# ---------------------------------------------------------------------------

_STATE_LABEL = {"active": "active", "paused": "paused", "empty_paused": "empty"}


async def _schedules_list_text_and_keyboard(
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the schedule list display for the user's currently selected channel."""
    user_ctx = await db.get_user_context(user_id)
    sel_ch_id = user_ctx.get("selected_channel_id")
    if sel_ch_id is None:
        return "No channel selected. Use /select to pick a channel first.", None

    channel = await db.get_channel_by_id_for_user(user_id, int(sel_ch_id))
    if channel is None:
        return "Selected channel not found.", None

    ch_name = str(channel.get("channel_name") or f"Channel {sel_ch_id}")
    schedules = await db.get_channel_schedules(int(channel["id"]))

    if not schedules:
        return (
            f"No schedules for '{ch_name}'.",
            InlineKeyboardMarkup([[InlineKeyboardButton("New schedule", callback_data="sm:new")]]),
        )

    rows: list[list[InlineKeyboardButton]] = []
    for s in schedules:
        s_id = int(s["id"])
        name = str(s.get("name") or f"Schedule {s_id}")
        state = str(s.get("state") or "")
        count = await db.get_queue_count(s_id)
        state_label = _STATE_LABEL.get(state, state)
        tz = str(s.get("timezone") or "UTC")
        pattern = s.get("pattern") or {}
        pattern_label = _pattern_summary(pattern, tz_name=tz)

        rows.append([InlineKeyboardButton(
            f"{name}  •  {state_label}  •  {count} queued",
            callback_data="sm:noop",
        )])
        rows.append([InlineKeyboardButton(
            pattern_label,
            callback_data="sm:noop",
        )])
        action_row: list[InlineKeyboardButton] = []
        if state == "active":
            action_row.append(InlineKeyboardButton("Pause", callback_data=f"sm:pause:{s_id}"))
        else:
            action_row.append(InlineKeyboardButton("Resume", callback_data=f"sm:resume:{s_id}"))
        action_row += [
            InlineKeyboardButton("Edit", callback_data=f"sm:edit:{s_id}"),
            InlineKeyboardButton("Set TZ", callback_data=f"sm:settp:{s_id}"),
            InlineKeyboardButton("Delete", callback_data=f"sm:rm:{s_id}"),
        ]
        rows.append(action_row)

    rows.append([InlineKeyboardButton("New schedule", callback_data="sm:new")])
    return f"Schedules for '{ch_name}':", InlineKeyboardMarkup(rows)


async def schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/schedules — show the schedule list with action buttons."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None or update.effective_chat is None:
        return ConversationHandler.END

    text, keyboard = await _schedules_list_text_and_keyboard(update.effective_user.id)
    if keyboard is None:
        await update.message.reply_text(text)
        return ConversationHandler.END

    sent = await update.message.reply_text(text, reply_markup=keyboard)
    context.user_data["sm_msg_id"] = sent.message_id
    context.user_data["sm_chat_id"] = update.effective_chat.id
    return SM_SHOWING


async def _refresh_list(user_id: int, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    """Edit the stored list message to show the current schedule state."""
    text, keyboard = await _schedules_list_text_and_keyboard(user_id)
    if query is not None:
        try:
            if keyboard:
                await query.edit_message_text(text, reply_markup=keyboard)
            else:
                await query.edit_message_text(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /schedules — inline action callbacks (SM_SHOWING state)
# ---------------------------------------------------------------------------

async def schedules_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sm:* inline keyboard callbacks."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return SM_SHOWING

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    # --- sm:noop ---
    if data == "sm:noop":
        return SM_SHOWING

    # --- sm:back — refresh list ---
    if data == "sm:back":
        await _refresh_list(user_id, context, query)
        return SM_SHOWING

    # --- sm:pause:{id} ---
    if data.startswith("sm:pause:"):
        try:
            s_id = int(data[9:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        await scheduling.pause(s_id, user_id=user_id)
        await _refresh_list(user_id, context, query)
        return SM_SHOWING

    # --- sm:resume:{id} ---
    if data.startswith("sm:resume:"):
        try:
            s_id = int(data[10:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        count = await db.get_queue_count(s_id)
        if count == 0:
            await query.answer(
                "Queue is empty — add posts with /bulk before resuming.", show_alert=True
            )
            return SM_SHOWING
        await scheduling.resume(s_id, user_id=user_id)
        await _refresh_list(user_id, context, query)
        return SM_SHOWING

    # --- sm:rm:{id} — show delete confirmation ---
    if data.startswith("sm:rm:"):
        try:
            s_id = int(data[6:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        n_posts = await db.get_queue_count(s_id)
        name = str(schedule.get("name") or f"Schedule {s_id}")
        lines = [f"Delete '{name}'?"]
        if n_posts:
            lines.append(f"This will also delete {n_posts} queued post(s).")
        try:
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Yes, delete", callback_data=f"sm:rmok:{s_id}"),
                    InlineKeyboardButton("Cancel", callback_data="sm:back"),
                ]]),
            )
        except Exception:
            pass
        return SM_SHOWING

    # --- sm:rmok:{id} — perform deletion ---
    if data.startswith("sm:rmok:"):
        try:
            s_id = int(data[8:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        await scheduling.delete(s_id, user_id=user_id)
        logger.info("User %s deleted schedule %s", user_id, s_id)
        await _refresh_list(user_id, context, query)
        return SM_SHOWING

    # --- sm:settp:{id} — ask for timezone ---
    if data.startswith("sm:settp:"):
        try:
            s_id = int(data[9:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        context.user_data["sm_settp_schedule_id"] = s_id
        try:
            await query.edit_message_text(
                f"Enter the timezone for '{schedule['name']}' (e.g. Europe/Amsterdam, UTC).\n\n"
                "/cancel to abort."
            )
        except Exception:
            pass
        return SM_WAIT_TZ_INPUT

    # --- sm:new — start new schedule wizard ---
    if data == "sm:new":
        user_ctx = await db.get_user_context(user_id)
        sel_ch_id = user_ctx.get("selected_channel_id")
        if sel_ch_id is None:
            await query.answer("Select a channel first with /select.", show_alert=True)
            return SM_SHOWING
        channel = await db.get_channel_by_id_for_user(user_id, int(sel_ch_id))
        if channel is None:
            await query.answer("Selected channel not found.", show_alert=True)
            return SM_SHOWING
        _clear_ns_state(context)
        context.user_data["ns_channel_db_id"] = int(channel["id"])
        context.user_data["ns_channel_name"] = str(channel["channel_name"])
        context.user_data["ns_timezone"] = await _effective_user_timezone_name(user_id)
        tz_name = context.user_data["ns_timezone"]
        try:
            await query.edit_message_text(
                f"New schedule for '{channel['channel_name']}'.\n"
                f"Timezone: {tz_name}\n\n"
                "Enter a schedule name (or /cancel)."
            )
        except Exception:
            pass
        return NS_WAIT_NAME

    # --- sm:edit:{id} — start edit schedule wizard ---
    if data.startswith("sm:edit:"):
        try:
            s_id = int(data[8:])
        except ValueError:
            return SM_SHOWING
        schedule = await db.get_schedule_for_user(user_id, s_id)
        if schedule is None:
            await query.answer("Schedule not found.", show_alert=True)
            return SM_SHOWING
        _clear_es_state(context)
        context.user_data["es_schedule_id"] = s_id
        context.user_data["es_current_name"] = schedule.get("name")
        context.user_data["es_current_pattern"] = schedule.get("pattern")
        context.user_data["es_timezone"] = str(schedule.get("timezone") or default_timezone_name())
        tz_name = context.user_data["es_timezone"]
        try:
            await query.edit_message_text(
                f"Editing '{schedule['name']}' (timezone: {tz_name}).\n\n"
                "What do you want to edit? Reply with: name or pattern\n\n"
                "/cancel to stop."
            )
        except Exception:
            pass
        return ES_WAIT_FIELD

    return SM_SHOWING


# ---------------------------------------------------------------------------
# SM_WAIT_TZ_INPUT — set schedule timezone
# ---------------------------------------------------------------------------

async def schedules_tz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle timezone text input for the Set TZ action."""
    msg = update.message
    if msg is None or update.effective_user is None:
        return SM_WAIT_TZ_INPUT

    raw = (msg.text or "").strip()
    if not raw:
        await msg.reply_text("Enter a timezone name (e.g. Europe/Amsterdam) or /cancel.")
        return SM_WAIT_TZ_INPUT

    if not is_valid_timezone(raw):
        await msg.reply_text(
            f"Unknown timezone: {raw!r}\n"
            "Use an IANA timezone name like Europe/Amsterdam, UTC, America/New_York."
        )
        return SM_WAIT_TZ_INPUT

    s_id = context.user_data.get("sm_settp_schedule_id")
    if s_id is None:
        await msg.reply_text("Session expired. Use /schedules to start again.")
        return ConversationHandler.END

    schedule = await db.get_schedule_for_user(update.effective_user.id, int(s_id))
    if schedule is None:
        await msg.reply_text("Schedule not found.")
        return ConversationHandler.END

    await scheduling.update_timezone(
        int(s_id), timezone_name=raw, user_id=update.effective_user.id
    )
    logger.info("User %s set timezone of schedule %s to %s", update.effective_user.id, s_id, raw)
    await msg.reply_text(f"Timezone for '{schedule['name']}' set to {raw}.")
    context.user_data.pop("sm_settp_schedule_id", None)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# New-schedule wizard (NS_*) — embedded in schedules_conversation_handler
# ---------------------------------------------------------------------------

async def newschedule_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Schedule name cannot be empty. Enter a name (or /cancel).")
        return NS_WAIT_NAME
    context.user_data["ns_name"] = name
    await update.message.reply_text(
        "Choose schedule type: interval, daily, weekly\n"
        "Reply with one of those words (or /cancel)."
    )
    return NS_WAIT_TYPE


async def newschedule_set_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    schedule_type = (update.message.text or "").strip().lower()
    if schedule_type not in {"interval", "daily", "weekly"}:
        await update.message.reply_text("Invalid type. Reply with: interval, daily, weekly")
        return NS_WAIT_TYPE
    context.user_data["ns_type"] = schedule_type
    if schedule_type == "interval":
        await update.message.reply_text("Enter interval (examples: 1h, 30m, 90).")
        return NS_WAIT_INTERVAL
    tz_name = str(context.user_data.get("ns_timezone") or default_timezone_name())
    if schedule_type == "daily":
        await update.message.reply_text(
            f"Enter times in {tz_name} (HH:MM) separated by commas.\n"
            "Example: 09:00,16:00"
        )
        return NS_WAIT_DAILY_TIMES
    await update.message.reply_text(
        "Enter weekdays separated by commas.\n"
        "Example: monday,tuesday,wednesday,thursday,friday"
    )
    return NS_WAIT_WEEKLY_DAYS


async def newschedule_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    parsed = _parse_interval_input(update.message.text or "")
    if parsed is None:
        await update.message.reply_text("Invalid interval. Try: 1h, 30m, or 90")
        return NS_WAIT_INTERVAL
    hours, minutes = parsed
    pattern: dict = {"type": "interval"}
    if hours:
        pattern["hours"] = hours
    if minutes:
        pattern["minutes"] = minutes
    return await _newschedule_finalize(update, context, pattern)


async def newschedule_set_daily_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    times = _parse_times_csv(update.message.text or "")
    if times is None:
        tz_name = str(context.user_data.get("ns_timezone") or default_timezone_name())
        await update.message.reply_text(
            f"Invalid times. Use HH:MM separated by commas (interpreted in {tz_name})."
        )
        return NS_WAIT_DAILY_TIMES
    return await _newschedule_finalize(update, context, {"type": "daily", "times": times})


async def newschedule_set_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    days = _parse_weekdays_csv(update.message.text or "")
    if days is None:
        await update.message.reply_text("Invalid weekdays. Use names like: monday,tuesday,friday")
        return NS_WAIT_WEEKLY_DAYS
    context.user_data["ns_days"] = days
    tz_name = str(context.user_data.get("ns_timezone") or default_timezone_name())
    await update.message.reply_text(
        f"Enter times in {tz_name} (HH:MM) separated by commas.\n"
        "Example: 12:00"
    )
    return NS_WAIT_WEEKLY_TIMES


async def newschedule_set_weekly_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    times = _parse_times_csv(update.message.text or "")
    if times is None:
        tz_name = str(context.user_data.get("ns_timezone") or default_timezone_name())
        await update.message.reply_text(
            f"Invalid times. Use HH:MM separated by commas (interpreted in {tz_name})."
        )
        return NS_WAIT_WEEKLY_TIMES
    days = context.user_data.get("ns_days") or []
    return await _newschedule_finalize(update, context, {"type": "weekly", "days": days, "times": times})


async def _newschedule_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, pattern: dict) -> int:
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END
    ok, reason = validate_schedule_pattern(pattern)
    if not ok:
        await update.message.reply_text(f"Schedule pattern invalid: {reason}")
        return ConversationHandler.END
    raw_ch = context.user_data.get("ns_channel_db_id")
    if raw_ch is None:
        await update.message.reply_text("Session expired. Use /schedules to start again.")
        return ConversationHandler.END
    channel_db_id = int(raw_ch)
    user_id = update.effective_user.id
    prior_ctx = await db.get_user_context(user_id)
    is_first_schedule = not prior_ctx.get("selected_schedule_id")
    name = str(context.user_data.get("ns_name"))
    tz_name = str(context.user_data.get("ns_timezone") or default_timezone_name())
    schedule = await scheduling.create(
        channel_db_id=channel_db_id,
        name=name,
        pattern=pattern,
        timezone_name=tz_name,
        state="paused",
    )
    await db.set_user_context(
        user_id=user_id,
        selected_channel_id=channel_db_id,
        selected_schedule_id=int(schedule["id"]),
    )
    confirmation = (
        f"Schedule '{name}' created.\n"
        f"Pattern: {_pattern_summary(pattern, tz_name=tz_name)}\n"
        "State: paused — use /schedules to resume when ready."
    )
    if is_first_schedule:
        confirmation += (
            "\n\nYou're all set! Send photos/videos to this chat to queue posts, "
            "or use /bulk for batch uploads."
        )
    await update.message.reply_text(confirmation)
    _clear_ns_state(context)
    logger.info("User %s created schedule %s", user_id, schedule["id"])
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Edit-schedule wizard (ES_*) — embedded in schedules_conversation_handler
# ---------------------------------------------------------------------------

async def editschedule_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    choice = (update.message.text or "").strip().lower()
    if choice == "name":
        await update.message.reply_text("Enter new schedule name.")
        return ES_WAIT_NAME
    if choice == "pattern":
        await update.message.reply_text(
            "Choose schedule type: interval, daily, weekly\n"
            "Reply with one of those words."
        )
        return ES_WAIT_TYPE
    await update.message.reply_text("Reply with: name or pattern")
    return ES_WAIT_FIELD


async def editschedule_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Name cannot be empty. Enter a name.")
        return ES_WAIT_NAME
    raw_id = context.user_data.get("es_schedule_id")
    if raw_id is None:
        await update.message.reply_text("Session expired. Use /schedules to start again.")
        return ConversationHandler.END
    s_id = int(raw_id)
    await scheduling.update_name(s_id, name=name, user_id=update.effective_user.id)
    await update.message.reply_text(f"Schedule renamed to '{name}'.")
    _clear_es_state(context)
    return ConversationHandler.END


async def editschedule_set_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    schedule_type = (update.message.text or "").strip().lower()
    if schedule_type not in {"interval", "daily", "weekly"}:
        await update.message.reply_text("Invalid type. Reply with: interval, daily, weekly")
        return ES_WAIT_TYPE
    context.user_data["es_type"] = schedule_type
    if schedule_type == "interval":
        await update.message.reply_text("Enter interval (examples: 1h, 30m, 90).")
        return ES_WAIT_INTERVAL
    tz_name = str(context.user_data.get("es_timezone") or default_timezone_name())
    if schedule_type == "daily":
        await update.message.reply_text(
            f"Enter times in {tz_name} (HH:MM) separated by commas.\n"
            "Example: 09:00,16:00"
        )
        return ES_WAIT_DAILY_TIMES
    await update.message.reply_text(
        "Enter weekdays separated by commas.\n"
        "Example: monday,tuesday,friday"
    )
    return ES_WAIT_WEEKLY_DAYS


async def editschedule_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    parsed = _parse_interval_input(update.message.text or "")
    if parsed is None:
        await update.message.reply_text("Invalid interval. Try: 1h, 30m, or 90")
        return ES_WAIT_INTERVAL
    hours, minutes = parsed
    pattern: dict = {"type": "interval"}
    if hours:
        pattern["hours"] = hours
    if minutes:
        pattern["minutes"] = minutes
    return await _editschedule_finalize(update, context, pattern)


async def editschedule_set_daily_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    times = _parse_times_csv(update.message.text or "")
    if times is None:
        tz_name = str(context.user_data.get("es_timezone") or default_timezone_name())
        await update.message.reply_text(
            f"Invalid times. Use HH:MM separated by commas (interpreted in {tz_name})."
        )
        return ES_WAIT_DAILY_TIMES
    return await _editschedule_finalize(update, context, {"type": "daily", "times": times})


async def editschedule_set_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    days = _parse_weekdays_csv(update.message.text or "")
    if days is None:
        await update.message.reply_text("Invalid weekdays. Use names like: monday,tuesday,friday")
        return ES_WAIT_WEEKLY_DAYS
    context.user_data["es_days"] = days
    tz_name = str(context.user_data.get("es_timezone") or default_timezone_name())
    await update.message.reply_text(
        f"Enter times in {tz_name} (HH:MM) separated by commas.\n"
        "Example: 12:00"
    )
    return ES_WAIT_WEEKLY_TIMES


async def editschedule_set_weekly_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END
    times = _parse_times_csv(update.message.text or "")
    if times is None:
        tz_name = str(context.user_data.get("es_timezone") or default_timezone_name())
        await update.message.reply_text(
            f"Invalid times. Use HH:MM separated by commas (interpreted in {tz_name})."
        )
        return ES_WAIT_WEEKLY_TIMES
    days = context.user_data.get("es_days") or []
    return await _editschedule_finalize(update, context, {"type": "weekly", "days": days, "times": times})


async def _editschedule_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, pattern: dict) -> int:
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END
    ok, reason = validate_schedule_pattern(pattern)
    if not ok:
        await update.message.reply_text(f"Schedule pattern invalid: {reason}")
        return ConversationHandler.END
    raw_id = context.user_data.get("es_schedule_id")
    if raw_id is None:
        await update.message.reply_text("Session expired. Use /schedules to start again.")
        return ConversationHandler.END
    s_id = int(raw_id)
    await scheduling.update_pattern(s_id, pattern, user_id=update.effective_user.id)
    tz_name = str(context.user_data.get("es_timezone") or default_timezone_name())
    await update.message.reply_text(
        f"Pattern updated: {_pattern_summary(pattern, tz_name=tz_name)}"
    )
    _clear_es_state(context)
    logger.info("User %s updated schedule %s", update.effective_user.id, s_id)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Shared cancel fallback
# ---------------------------------------------------------------------------

async def schedule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_ns_state(context)
    _clear_es_state(context)
    context.user_data.pop("sm_settp_schedule_id", None)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Legacy command (kept for tests / power users; not registered in bot.py)
# ---------------------------------------------------------------------------

async def setscheduletimezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a schedule's timezone via command args (legacy, power-user)."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    schedule_id: int | None = None
    tz_arg: str | None = None
    if len(context.args) == 2:
        schedule_id = _parse_schedule_id(context.args[0])
        tz_arg = context.args[1]
    elif len(context.args) == 1:
        tz_arg = context.args[0]
        raw = (await db.get_user_context(user_id)).get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
    else:
        await update.message.reply_text(
            "Usage: /setscheduletimezone <schedule_id> <timezone>\n"
            "Or use /schedules > Set TZ for the guided flow."
        )
        return
    if schedule_id is None:
        await update.message.reply_text(
            "No schedule selected. Use /select first, or provide a schedule_id."
        )
        return
    schedule = await db.get_schedule_for_user(user_id, schedule_id)
    if schedule is None:
        await update.message.reply_text("Schedule not found or not owned by you.")
        return
    raw_tz = (tz_arg or "").strip()
    if raw_tz.lower() in {"default", "reset", "clear"}:
        raw_tz = await _effective_user_timezone_name(user_id)
    if not is_valid_timezone(raw_tz):
        await update.message.reply_text(f"Unknown timezone: {raw_tz!r}")
        return
    await scheduling.update_timezone(
        schedule_id, timezone_name=raw_tz, user_id=user_id
    )
    await update.message.reply_text(f"Schedule {schedule_id} timezone set to {raw_tz}.")


# ---------------------------------------------------------------------------
# The unified ConversationHandler
# ---------------------------------------------------------------------------

_MSG_HANDLER = filters.TEXT & ~filters.COMMAND

schedules_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("schedules", schedules_command)],
    allow_reentry=True,
    states={
        SM_SHOWING: [CallbackQueryHandler(schedules_callback, pattern=r"^sm:")],
        SM_WAIT_TZ_INPUT: [MessageHandler(_MSG_HANDLER, schedules_tz_handler)],
        # New-schedule wizard
        NS_WAIT_NAME: [MessageHandler(_MSG_HANDLER, newschedule_set_name)],
        NS_WAIT_TYPE: [MessageHandler(_MSG_HANDLER, newschedule_set_type)],
        NS_WAIT_INTERVAL: [MessageHandler(_MSG_HANDLER, newschedule_set_interval)],
        NS_WAIT_DAILY_TIMES: [MessageHandler(_MSG_HANDLER, newschedule_set_daily_times)],
        NS_WAIT_WEEKLY_DAYS: [MessageHandler(_MSG_HANDLER, newschedule_set_weekly_days)],
        NS_WAIT_WEEKLY_TIMES: [MessageHandler(_MSG_HANDLER, newschedule_set_weekly_times)],
        # Edit-schedule wizard
        ES_WAIT_FIELD: [MessageHandler(_MSG_HANDLER, editschedule_choose_field)],
        ES_WAIT_NAME: [MessageHandler(_MSG_HANDLER, editschedule_set_name)],
        ES_WAIT_TYPE: [MessageHandler(_MSG_HANDLER, editschedule_set_type)],
        ES_WAIT_INTERVAL: [MessageHandler(_MSG_HANDLER, editschedule_set_interval)],
        ES_WAIT_DAILY_TIMES: [MessageHandler(_MSG_HANDLER, editschedule_set_daily_times)],
        ES_WAIT_WEEKLY_DAYS: [MessageHandler(_MSG_HANDLER, editschedule_set_weekly_days)],
        ES_WAIT_WEEKLY_TIMES: [MessageHandler(_MSG_HANDLER, editschedule_set_weekly_times)],
    },
    fallbacks=[CommandHandler("cancel", schedule_cancel)],
    name="schedules",
    persistent=True,
)
