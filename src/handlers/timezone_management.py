"""User timezone preference commands and guided timezone selection."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import queries as db
from handlers.common import ensure_user_record
from utils.tz import default_timezone_name, is_valid_timezone, suggest_timezones


def _unknown_timezone_message(raw: str, *, include_guided_hint: bool = True) -> str:
    """Build the user-facing 'unknown timezone' reply.

    Uses `suggest_timezones` to nudge the user toward a likely typo
    correction when one exists. Caller decides whether to append the
    `/timezone` guided-selection hint (keyboard-originated errors
    already pop the keyboard back open, so the hint is redundant there).
    """
    suggestions = suggest_timezones(raw)
    head = f"Unknown timezone: {raw!r}"
    if suggestions:
        head += f"\nDid you mean: {', '.join(suggestions)}?"
    tail = "\nUse an IANA timezone name like Europe/Amsterdam or UTC."
    if include_guided_hint:
        tail += "\nOr use /timezone for guided selection."
    return head + tail


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback data tokens
# ---------------------------------------------------------------------------
_CB_REGIONS = "tz:regions"
_CB_REGION_PREFIX = "tz:r:"  # + region key
_CB_SET_PREFIX = "tz:s:"  # + IANA name (use data[len(_CB_SET_PREFIX):] to extract)
_CB_MANUAL = "tz:manual"

# ---------------------------------------------------------------------------
# Region / timezone data
# ---------------------------------------------------------------------------
_REGIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "africa": (
        "Africa",
        [
            ("Cairo", "Africa/Cairo"),
            ("Lagos", "Africa/Lagos"),
            ("Nairobi", "Africa/Nairobi"),
            ("Johannesburg", "Africa/Johannesburg"),
            ("Casablanca", "Africa/Casablanca"),
            ("Accra", "Africa/Accra"),
        ],
    ),
    "americas": (
        "Americas",
        [
            ("New York", "America/New_York"),
            ("Chicago", "America/Chicago"),
            ("Denver", "America/Denver"),
            ("Los Angeles", "America/Los_Angeles"),
            ("Toronto", "America/Toronto"),
            ("Sao Paulo", "America/Sao_Paulo"),
            ("Mexico City", "America/Mexico_City"),
            ("Buenos Aires", "America/Argentina/Buenos_Aires"),
        ],
    ),
    "asia": (
        "Asia & Pacific",
        [
            ("Dubai", "Asia/Dubai"),
            ("Kolkata", "Asia/Kolkata"),
            ("Bangkok", "Asia/Bangkok"),
            ("Singapore", "Asia/Singapore"),
            ("Shanghai", "Asia/Shanghai"),
            ("Tokyo", "Asia/Tokyo"),
            ("Seoul", "Asia/Seoul"),
            ("Sydney", "Australia/Sydney"),
        ],
    ),
    "europe": (
        "Europe",
        [
            ("Amsterdam", "Europe/Amsterdam"),
            ("Berlin", "Europe/Berlin"),
            ("London", "Europe/London"),
            ("Moscow", "Europe/Moscow"),
            ("Paris", "Europe/Paris"),
            ("Rome", "Europe/Rome"),
            ("Stockholm", "Europe/Stockholm"),
            ("Zurich", "Europe/Zurich"),
        ],
    ),
    "utc": (
        "UTC / Other",
        [
            ("UTC", "UTC"),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _regions_keyboard() -> InlineKeyboardMarkup:
    """Return the top-level region selection keyboard."""
    rows = []
    for key, (label, _) in _REGIONS.items():
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"{_CB_REGION_PREFIX}{key}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "Enter manually (/settimezone)", callback_data=_CB_MANUAL
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _region_keyboard(region_key: str) -> InlineKeyboardMarkup | None:
    """Return the timezone selection keyboard for a region, or None if unknown."""
    if region_key not in _REGIONS:
        return None
    _, timezones = _REGIONS[region_key]
    rows: list[list[InlineKeyboardButton]] = []
    # Two buttons per row.
    pair: list[InlineKeyboardButton] = []
    for label, iana in timezones:
        pair.append(
            InlineKeyboardButton(label, callback_data=f"{_CB_SET_PREFIX}{iana}")
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(
        [
            InlineKeyboardButton(
                "Enter manually (/settimezone)", callback_data=_CB_MANUAL
            )
        ]
    )
    rows.append([InlineKeyboardButton("< Back", callback_data=_CB_REGIONS)])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Public helper: send a fresh timezone prompt message
# ---------------------------------------------------------------------------


async def send_timezone_prompt(*, chat_id: int, bot: Any) -> None:
    """Send a new timezone selection message to the given chat."""
    await bot.send_message(
        chat_id=chat_id,
        text="Select your timezone region:",
        reply_markup=_regions_keyboard(),
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the guided timezone selection flow."""
    await ensure_user_record(update, context)
    if update.message is None:
        return
    await update.message.reply_text(
        "Select your timezone region:",
        reply_markup=_regions_keyboard(),
    )


async def gettimezone_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the user's configured timezone (or default)."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    configured = await db.get_user_timezone(update.effective_user.id)
    effective = configured or default_timezone_name()

    if not configured:
        text = (
            f"Your timezone: {effective} (default)\n"
            f"Set it with /timezone or /settimezone <IANA name>\n"
            f"Example: /settimezone Europe/Amsterdam"
        )
    else:
        text = (
            f"Your timezone: {effective}\n"
            f"Change it with /timezone or /settimezone <IANA name>\n"
            f"Reset to default with /settimezone default"
        )

    await update.message.reply_text(text)


async def settimezone_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Set the user's timezone by IANA name (power-user text command)."""
    await ensure_user_record(update, context)
    if update.message is None or update.effective_user is None:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /settimezone <timezone>\n"
            "Example: /settimezone Europe/Amsterdam\n"
            "Reset: /settimezone default\n\n"
            "Or use /timezone for guided selection."
        )
        return

    raw = (context.args[0] or "").strip()
    lowered = raw.lower()
    if lowered in {"default", "clear", "reset"}:
        await db.set_user_timezone(update.effective_user.id, None)
        effective = default_timezone_name()
        await update.message.reply_text(
            f"Timezone cleared. Using default: {effective}."
        )
        return

    if not is_valid_timezone(raw):
        await update.message.reply_text(_unknown_timezone_message(raw))
        return

    await db.set_user_timezone(update.effective_user.id, raw)
    await update.message.reply_text(
        f"Timezone set to {raw}.\n"
        f"This will be used as the default timezone for new schedules."
    )
    logger.info("User %s set timezone to %r", update.effective_user.id, raw)


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------


async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all tz:* inline keyboard callbacks."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    # Back to top-level region list.
    if data == _CB_REGIONS:
        try:
            await query.edit_message_text(
                "Select your timezone region:",
                reply_markup=_regions_keyboard(),
            )
        except Exception:
            pass
        return

    # Drill into a region.
    if data.startswith(_CB_REGION_PREFIX):
        region_key = data[len(_CB_REGION_PREFIX) :]
        keyboard = _region_keyboard(region_key)
        if keyboard is None:
            await query.edit_message_text(
                "Unknown region. Please try again.", reply_markup=_regions_keyboard()
            )
            return
        region_label = _REGIONS[region_key][0]
        try:
            await query.edit_message_text(
                f"{region_label} — select your timezone:",
                reply_markup=keyboard,
            )
        except Exception:
            pass
        return

    # Set a specific timezone.
    if data.startswith(_CB_SET_PREFIX):
        iana_name = data[len(_CB_SET_PREFIX) :]
        if not is_valid_timezone(iana_name):
            try:
                await query.edit_message_text(
                    _unknown_timezone_message(iana_name, include_guided_hint=False)
                    + "\n\nPlease select again.",
                    reply_markup=_regions_keyboard(),
                )
            except Exception:
                pass
            return

        await db.set_user_timezone(user_id, iana_name)
        logger.info("User %s set timezone to %r (via inline)", user_id, iana_name)
        try:
            await query.edit_message_text(
                f"Timezone set to {iana_name}.\n"
                f"All date and time displays will use this timezone.\n"
                f"You can change it anytime with /timezone."
            )
        except Exception:
            pass
        return

    # Manual entry instructions.
    if data == _CB_MANUAL:
        try:
            await query.edit_message_text(
                "To set a custom timezone, send:\n"
                "/settimezone <IANA name>\n\n"
                "Examples:\n"
                "  /settimezone Europe/Amsterdam\n"
                "  /settimezone Asia/Kolkata\n"
                "  /settimezone America/New_York\n\n"
                "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("< Back", callback_data=_CB_REGIONS),
                        ]
                    ]
                ),
            )
        except Exception:
            pass
        return
