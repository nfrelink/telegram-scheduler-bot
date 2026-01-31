"""Schedule creation and management commands."""

from __future__ import annotations

import logging
import os

from telegram import Update
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
from handlers.verification import resolve_channel_id
from scheduler.timing import WEEKDAY_NAME_TO_INT, parse_time_string, validate_schedule_pattern
from utils.tg_text import Segment, render

logger = logging.getLogger(__name__)


# Conversation state enums
(
    NS_WAIT_NAME,
    NS_WAIT_TYPE,
    NS_WAIT_INTERVAL,
    NS_WAIT_DAILY_TIMES,
    NS_WAIT_WEEKLY_DAYS,
    NS_WAIT_WEEKLY_TIMES,
) = range(6)

(
    ES_WAIT_FIELD,
    ES_WAIT_NAME,
    ES_WAIT_TYPE,
    ES_WAIT_INTERVAL,
    ES_WAIT_DAILY_TIMES,
    ES_WAIT_WEEKLY_DAYS,
    ES_WAIT_WEEKLY_TIMES,
) = range(7)


def _default_timezone_name() -> str:
    return os.getenv("DEFAULT_TIMEZONE", "UTC") or "UTC"


def _parse_schedule_id(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _parse_interval_input(text: str) -> tuple[int, int] | None:
    """Parse an interval like '1h', '30m', or '90' (minutes)."""
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return None

    if raw.endswith("h"):
        n = _parse_int(raw[:-1])
        if n is None or n <= 0:
            return None
        return n, 0

    if raw.endswith("m"):
        n = _parse_int(raw[:-1])
        if n is None or n <= 0:
            return None
        return 0, n

    n = _parse_int(raw)
    if n is None or n <= 0:
        return None
    return 0, n


def _parse_times_csv(text: str) -> list[str] | None:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return None
    if not all(parse_time_string(p) for p in parts):
        return None
    # Normalize to HH:MM
    normalized: list[str] = []
    for p in parts:
        hour, minute = parse_time_string(p) or (0, 0)
        normalized.append(f"{hour:02d}:{minute:02d}")
    return normalized


def _parse_weekdays_csv(text: str) -> list[str] | None:
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    if not parts:
        return None
    if not all(p in WEEKDAY_NAME_TO_INT for p in parts):
        return None
    # Preserve user order but de-duplicate
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _pattern_summary(pattern: dict) -> str:
    schedule_type = pattern.get("type")
    if schedule_type == "interval":
        h = int(pattern.get("hours", 0) or 0)
        m = int(pattern.get("minutes", 0) or 0)
        return f"interval ({h}h {m}m)"
    if schedule_type == "daily":
        times = ", ".join(pattern.get("times", []))
        return f"daily ({times} UTC)"
    if schedule_type == "weekly":
        days = ", ".join(pattern.get("days", []))
        times = ", ".join(pattern.get("times", []))
        return f"weekly ({days} at {times} UTC)"
    return "unknown"


# --- /newschedule conversation ----------------------------------------------


async def newschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin interactive schedule creation."""
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    channel: dict | None = None
    telegram_channel_id: str | None = None

    if context.args and len(context.args) == 1:
        telegram_channel_id = await resolve_channel_id(context, context.args[0])
        if telegram_channel_id is None:
            await update.message.reply_text(
                "Could not resolve that channel. Use /listchannels and copy the channel id."
            )
            return ConversationHandler.END

        channel = await db.get_channel_by_telegram_id(telegram_channel_id)
        if channel is None or int(channel["user_id"]) != update.effective_user.id:
            await update.message.reply_text(
                "Channel not found or you don't have permission to create schedules for it.\n"
                "Use /listchannels to see your verified channels."
            )
            return ConversationHandler.END

        # Using an explicit channel also updates selection (clears schedule selection).
        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(channel["id"]),
            selected_schedule_id=None,
        )
    else:
        # No argument: fall back to selected channel.
        user_ctx = await db.get_user_context(update.effective_user.id)
        selected_channel_id = user_ctx.get("selected_channel_id")
        if selected_channel_id is None:
            await update.message.reply_text(
                "Usage: /newschedule <channel_id>\n"
                "Example: /newschedule -1001234567890\n\n"
                "Tip: select a default channel first:\n"
                "- /listchannels\n"
                "- /selectchannel <channel_id>"
            )
            return ConversationHandler.END

        channel = await db.get_channel_by_id(int(selected_channel_id))
        if channel is None or int(channel["user_id"]) != update.effective_user.id:
            await update.message.reply_text(
                "Your selected channel is missing or not owned by you.\n"
                "Use /listchannels and /selectchannel again."
            )
            return ConversationHandler.END

        telegram_channel_id = str(channel["channel_id"])

    context.user_data["ns_channel_db_id"] = int(channel["id"])
    context.user_data["ns_channel_name"] = str(channel["channel_name"])

    # Remind which channel we are creating a schedule for.
    details = await db.get_user_context_details(update.effective_user.id)
    header = selection_segments(details)
    msg_text, msg_entities = render([*header, Segment("\n\nEnter a schedule name (or /cancel).")])
    await update.message.reply_text(msg_text, entities=msg_entities)
    return NS_WAIT_NAME


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
        await update.message.reply_text(
            "Enter interval (examples: 1h, 30m, 90)."
        )
        return NS_WAIT_INTERVAL

    if schedule_type == "daily":
        await update.message.reply_text(
            "Enter times in UTC (HH:MM) separated by commas.\n"
            "Example: 09:00,16:00\n"
            "Note: times are interpreted as UTC (not your local timezone)."
        )
        return NS_WAIT_DAILY_TIMES

    if schedule_type == "weekly":
        await update.message.reply_text(
            "Enter weekdays separated by commas.\n"
            "Example: monday,tuesday,wednesday,thursday,friday"
        )
        return NS_WAIT_WEEKLY_DAYS

    await update.message.reply_text("Invalid type. Reply with: interval, daily, weekly")
    return NS_WAIT_TYPE


async def newschedule_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    parsed = _parse_interval_input(update.message.text or "")
    if parsed is None:
        await update.message.reply_text("Invalid interval. Try: 1h, 30m, or 90")
        return NS_WAIT_INTERVAL

    hours, minutes = parsed
    pattern = {"type": "interval"}
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
        await update.message.reply_text("Invalid times. Use UTC HH:MM separated by commas.")
        return NS_WAIT_DAILY_TIMES

    pattern = {"type": "daily", "times": times}
    return await _newschedule_finalize(update, context, pattern)


async def newschedule_set_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    days = _parse_weekdays_csv(update.message.text or "")
    if days is None:
        await update.message.reply_text(
            "Invalid weekdays. Use names like: monday,tuesday,wednesday"
        )
        return NS_WAIT_WEEKLY_DAYS

    context.user_data["ns_days"] = days
    await update.message.reply_text(
        "Enter times in UTC (HH:MM) separated by commas.\n"
        "Example: 12:00\n"
        "Note: times are interpreted as UTC (not your local timezone)."
    )
    return NS_WAIT_WEEKLY_TIMES


async def newschedule_set_weekly_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    times = _parse_times_csv(update.message.text or "")
    if times is None:
        await update.message.reply_text("Invalid times. Use UTC HH:MM separated by commas.")
        return NS_WAIT_WEEKLY_TIMES

    days = context.user_data.get("ns_days") or []
    pattern = {"type": "weekly", "days": days, "times": times}
    return await _newschedule_finalize(update, context, pattern)


async def _newschedule_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, pattern: dict) -> int:
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    ok, reason = validate_schedule_pattern(pattern)
    if not ok:
        await update.message.reply_text(f"Schedule pattern invalid: {reason}")
        return ConversationHandler.END

    channel_db_id = int(context.user_data.get("ns_channel_db_id"))
    name = str(context.user_data.get("ns_name"))
    timezone_name = _default_timezone_name()

    schedule = await db.create_schedule(
        channel_db_id=channel_db_id,
        name=name,
        pattern=pattern,
        timezone_name=timezone_name,
        state="paused",
    )

    channel_name = str(context.user_data.get("ns_channel_name") or channel_db_id)

    # Automatically select the new schedule.
    await db.set_user_context(
        user_id=update.effective_user.id,
        selected_channel_id=channel_db_id,
        selected_schedule_id=int(schedule["id"]),
    )

    segments = [
        Segment("Schedule created.\n"),
        Segment("ID: "),
        Segment(str(schedule["id"]), code=True),
        Segment("\nChannel: "),
        Segment(channel_name),
        Segment("\nPattern: "),
        Segment(_pattern_summary(pattern)),
        Segment("\nState: paused\n"),
        Segment("Next steps: /resumeschedule "),
        Segment(str(schedule["id"]), code=True),
        Segment("\nQueue: /viewqueue "),
        Segment(str(schedule["id"]), code=True),
        Segment("\nAdd posts: /bulk "),
        Segment(str(schedule["id"]), code=True),
        Segment("\n\n"),
        *selection_segments(await db.get_user_context_details(update.effective_user.id)),
    ]
    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)

    _clear_new_schedule_state(context)
    logger.info(
        "User %s created schedule id=%s for channel_db_id=%s",
        update.effective_user.id,
        schedule["id"],
        channel_db_id,
    )
    return ConversationHandler.END


async def schedule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    _clear_new_schedule_state(context)
    _clear_edit_schedule_state(context)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def _clear_new_schedule_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("ns_"):
            context.user_data.pop(key, None)


def _clear_edit_schedule_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in list(context.user_data.keys()):
        if key.startswith("es_"):
            context.user_data.pop(key, None)


new_schedule_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("newschedule", newschedule_start)],
    states={
        NS_WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_name)],
        NS_WAIT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_type)],
        NS_WAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_interval)],
        NS_WAIT_DAILY_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_daily_times)],
        NS_WAIT_WEEKLY_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_weekly_days)],
        NS_WAIT_WEEKLY_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, newschedule_set_weekly_times)],
    },
    fallbacks=[CommandHandler("cancel", schedule_cancel)],
)


# --- Non-conversation schedule commands -------------------------------------


async def list_schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    channel: dict | None = None
    telegram_channel_id: str | None = None

    if context.args and len(context.args) == 1:
        telegram_channel_id = await resolve_channel_id(context, context.args[0])
        if telegram_channel_id is None:
            await update.message.reply_text("Could not resolve that channel.")
            return

        channel = await db.get_channel_by_telegram_id(telegram_channel_id)
        if channel is None or int(channel["user_id"]) != update.effective_user.id:
            await update.message.reply_text("Channel not found or not owned by you.")
            return

        await db.set_user_context(
            user_id=update.effective_user.id,
            selected_channel_id=int(channel["id"]),
            selected_schedule_id=None,
        )
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        selected_channel_id = user_ctx.get("selected_channel_id")
        if selected_channel_id is None:
            await update.message.reply_text(
                "Usage: /listschedules <channel_id>\n"
                "Example: /listschedules -1001234567890\n\n"
                "Tip: select a default channel first:\n"
                "- /listchannels\n"
                "- /selectchannel <channel_id>"
            )
            return

        channel = await db.get_channel_by_id(int(selected_channel_id))
        if channel is None or int(channel["user_id"]) != update.effective_user.id:
            await update.message.reply_text(
                "Your selected channel is missing or not owned by you.\n"
                "Use /listchannels and /selectchannel again."
            )
            return

        telegram_channel_id = str(channel["channel_id"])

    schedules = await db.get_channel_schedules(int(channel["id"]))
    if not schedules:
        details = await db.get_user_context_details(update.effective_user.id)
        msg_text, msg_entities = render(
            [
                Segment("No schedules for this channel yet. Use /newschedule to create one.\n\n"),
                *selection_segments(details),
            ]
        )
        await update.message.reply_text(msg_text, entities=msg_entities)
        return

    segments: list[Segment] = [
        Segment("Schedules for channel '"),
        Segment(str(channel["channel_name"])),
        Segment("' ("),
        Segment(str(telegram_channel_id), code=True),
        Segment("):\n"),
    ]
    for s in schedules:
        pattern = s.get("pattern") or {}
        segments += [
            Segment("- "),
            Segment(str(s["id"]), code=True),
            Segment(": "),
            Segment(str(s["name"])),
            Segment(" ["),
            Segment(str(s["state"])),
            Segment("] "),
            Segment(_pattern_summary(pattern)),
            Segment("\n"),
        ]

    segments += [Segment("\nTip: set a default schedule with /selectschedule "), Segment(str(schedules[0]["id"]), code=True), Segment(".\n\n")]
    segments += selection_segments(await db.get_user_context_details(update.effective_user.id))

    text, entities = render(segments)
    await update.message.reply_text(text, entities=entities)


async def pause_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args and len(context.args) == 1:
        schedule_id = _parse_schedule_id(context.args[0])
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
            "Usage: /pauseschedule <schedule_id>\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
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

    await db.update_schedule_state(schedule_id, "paused")
    details = await db.get_user_context_details(update.effective_user.id)
    text, entities = render(
        [
            Segment("Schedule "),
            Segment(str(schedule_id), code=True),
            Segment(" paused.\n\n"),
            *selection_segments(details),
        ]
    )
    await update.message.reply_text(text, entities=entities)


async def resume_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args and len(context.args) == 1:
        schedule_id = _parse_schedule_id(context.args[0])
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
            "Usage: /resumeschedule <schedule_id>\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
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

    await db.update_schedule_state(schedule_id, "active")
    details = await db.get_user_context_details(update.effective_user.id)
    text, entities = render(
        [
            Segment("Schedule "),
            Segment(str(schedule_id), code=True),
            Segment(" resumed.\n\n"),
            *selection_segments(details),
        ]
    )
    await update.message.reply_text(text, entities=entities)


async def delete_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    schedule_id: int | None = None
    used_selected = False
    if context.args and len(context.args) == 1:
        schedule_id = _parse_schedule_id(context.args[0])
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
            "Usage: /deleteschedule <schedule_id>\n"
            "Tip: select a default schedule with /selectschedule <schedule_id>."
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

    await db.delete_schedule(schedule_id)
    details = await db.get_user_context_details(update.effective_user.id)
    text, entities = render(
        [
            Segment("Schedule "),
            Segment(str(schedule_id), code=True),
            Segment(" deleted.\n\n"),
            *selection_segments(details),
        ]
    )
    await update.message.reply_text(text, entities=entities)


async def copy_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user_record(update, context)

    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text("Usage: /copyschedule <schedule_id> <target_channel_id>")
        return

    source_id = _parse_schedule_id(context.args[0])
    if source_id is None:
        await update.message.reply_text("Invalid source schedule id.")
        return

    source = await db.get_schedule_for_user(update.effective_user.id, source_id)
    if source is None:
        await update.message.reply_text("Source schedule not found or not owned by you.")
        return

    target_channel_id = await resolve_channel_id(context, context.args[1])
    if target_channel_id is None:
        await update.message.reply_text("Could not resolve target channel.")
        return

    target_channel = await db.get_channel_by_telegram_id(target_channel_id)
    if target_channel is None or int(target_channel["user_id"]) != update.effective_user.id:
        await update.message.reply_text("Target channel not found or not owned by you.")
        return

    new_schedule = await db.create_schedule(
        channel_db_id=int(target_channel["id"]),
        name=str(source["name"]),
        pattern=dict(source["pattern"]),
        timezone_name=str(source.get("timezone") or _default_timezone_name()),
        state="paused",
    )

    text, entities = render(
        [
            Segment("Schedule copied.\nNew schedule ID: "),
            Segment(str(new_schedule["id"]), code=True),
            Segment("\nState: paused"),
        ]
    )
    await update.message.reply_text(text, entities=entities)


# --- /editschedule conversation ---------------------------------------------


async def editschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    schedule_id: int | None = None
    used_selected = False
    if context.args and len(context.args) == 1:
        schedule_id = _parse_schedule_id(context.args[0])
        if schedule_id is None:
            await update.message.reply_text("Invalid schedule id.")
            return ConversationHandler.END
    else:
        user_ctx = await db.get_user_context(update.effective_user.id)
        raw = user_ctx.get("selected_schedule_id")
        schedule_id = int(raw) if raw is not None else None
        used_selected = True

    if schedule_id is None:
        await update.message.reply_text(
            "Usage: /editschedule <schedule_id>\n"
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

    context.user_data["es_schedule_id"] = schedule_id
    context.user_data["es_current_name"] = schedule.get("name")
    context.user_data["es_current_pattern"] = schedule.get("pattern")

    details = await db.get_user_context_details(update.effective_user.id)
    text, entities = render(
        [
            Segment("Editing schedule "),
            Segment(str(schedule_id), code=True),
            Segment(".\n\n"),
            *selection_segments(details),
            Segment("\n\nWhat do you want to edit? Reply with: name or pattern\nOr /cancel to stop."),
        ]
    )
    await update.message.reply_text(text, entities=entities)
    return ES_WAIT_FIELD


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

    await update.message.reply_text("Invalid choice. Reply with: name or pattern")
    return ES_WAIT_FIELD


async def editschedule_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Name cannot be empty. Enter a name.")
        return ES_WAIT_NAME

    schedule_id = int(context.user_data.get("es_schedule_id"))
    await db.update_schedule_name(schedule_id, name=name)
    text, entities = render([Segment("Schedule "), Segment(str(schedule_id), code=True), Segment(" renamed.")])
    await update.message.reply_text(text, entities=entities)
    _clear_edit_schedule_state(context)
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

    if schedule_type == "daily":
        await update.message.reply_text(
            "Enter times in UTC (HH:MM) separated by commas.\n"
            "Example: 09:00,16:00\n"
            "Note: times are interpreted as UTC (not your local timezone)."
        )
        return ES_WAIT_DAILY_TIMES

    if schedule_type == "weekly":
        await update.message.reply_text(
            "Enter weekdays separated by commas.\n"
            "Example: monday,tuesday,wednesday,thursday,friday"
        )
        return ES_WAIT_WEEKLY_DAYS

    await update.message.reply_text("Invalid type. Reply with: interval, daily, weekly")
    return ES_WAIT_TYPE


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
        await update.message.reply_text("Invalid times. Use UTC HH:MM separated by commas.")
        return ES_WAIT_DAILY_TIMES

    pattern = {"type": "daily", "times": times}
    return await _editschedule_finalize(update, context, pattern)


async def editschedule_set_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    days = _parse_weekdays_csv(update.message.text or "")
    if days is None:
        await update.message.reply_text(
            "Invalid weekdays. Use names like: monday,tuesday,wednesday"
        )
        return ES_WAIT_WEEKLY_DAYS

    context.user_data["es_days"] = days
    await update.message.reply_text(
        "Enter times in UTC (HH:MM) separated by commas.\n"
        "Example: 12:00\n"
        "Note: times are interpreted as UTC (not your local timezone)."
    )
    return ES_WAIT_WEEKLY_TIMES


async def editschedule_set_weekly_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ensure_user_record(update, context)
    if update.message is None:
        return ConversationHandler.END

    times = _parse_times_csv(update.message.text or "")
    if times is None:
        await update.message.reply_text("Invalid times. Use UTC HH:MM separated by commas.")
        return ES_WAIT_WEEKLY_TIMES

    days = context.user_data.get("es_days") or []
    pattern = {"type": "weekly", "days": days, "times": times}
    return await _editschedule_finalize(update, context, pattern)


async def _editschedule_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, pattern: dict) -> int:
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    ok, reason = validate_schedule_pattern(pattern)
    if not ok:
        await update.message.reply_text(f"Schedule pattern invalid: {reason}")
        return ConversationHandler.END

    schedule_id = int(context.user_data.get("es_schedule_id"))
    await db.update_schedule_pattern(schedule_id, pattern)

    text, entities = render(
        [
            Segment("Schedule "),
            Segment(str(schedule_id), code=True),
            Segment(" updated.\nPattern: "),
            Segment(_pattern_summary(pattern)),
        ]
    )
    await update.message.reply_text(text, entities=entities)

    _clear_edit_schedule_state(context)
    logger.info("User %s updated schedule id=%s", update.effective_user.id, schedule_id)
    return ConversationHandler.END


edit_schedule_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("editschedule", editschedule_start)],
    states={
        ES_WAIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_choose_field)],
        ES_WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_name)],
        ES_WAIT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_type)],
        ES_WAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_interval)],
        ES_WAIT_DAILY_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_daily_times)],
        ES_WAIT_WEEKLY_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_weekly_days)],
        ES_WAIT_WEEKLY_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, editschedule_set_weekly_times)],
    },
    fallbacks=[CommandHandler("cancel", schedule_cancel)],
)

