"""Admin DM helper.

Single entry point (`notify_admin`) used by `bot.error_handler`, the
scheduler engine's pause-detection sites, and the heartbeat. Centralises:

    - the `ADMIN_USER_ID` lookup (bot does nothing if unset),
    - signature-keyed debouncing (don't carpet-bomb on a flapping schedule),
    - Telegram message-length truncation (`_TELEGRAM_TEXT_LIMIT`),
    - the `(label, value)` -> formatted text shape every caller already wants.

This module is the one approved exception to the "no Telegram imports in
services" rule documented in `services/__init__.py`: the whole point of the
module is to talk to the bot.

Debounce state is a module-level dict, scoped per process. That matches the
process model (one bot, one event loop) and what `bot.py` did before this
extraction. Tests reset it via the exposed helper.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Sequence

from telegram.ext import ExtBot

logger = logging.getLogger(__name__)

_TELEGRAM_TEXT_LIMIT = 4096
_DEFAULT_DEBOUNCE_SECONDS = 60

# Per-process map: debounce key -> monotonic timestamp of last successful send.
_LAST_SENT_AT: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Env lookups
# ---------------------------------------------------------------------------


def _get_admin_user_id() -> int | None:
    raw = os.getenv("ADMIN_USER_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not an integer")
        return None


def _get_debounce_seconds() -> int:
    """Read `ADMIN_ERROR_DM_DEBOUNCE_SECONDS`. Negative values clamp to 0
    (debouncing disabled) so `notify_admin` can rely on `>= 0`."""
    raw = os.getenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS")
    if not raw:
        return _DEFAULT_DEBOUNCE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("ADMIN_ERROR_DM_DEBOUNCE_SECONDS is not an integer")
        return _DEFAULT_DEBOUNCE_SECONDS


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


def _should_send(key: str) -> bool:
    """Decide whether to send for `key` now, recording the send if yes.

    Debounce of 0 disables suppression entirely. Otherwise the same `key`
    can only fire once per debounce window.
    """
    debounce_seconds = _get_debounce_seconds()
    if debounce_seconds <= 0:
        return True

    now = time.monotonic()
    last = _LAST_SENT_AT.get(key)
    if last is not None and (now - last) < debounce_seconds:
        return False

    _LAST_SENT_AT[key] = now
    return True


def reset_debounce_state() -> None:
    """Clear the per-process debounce map. Test-only."""
    _LAST_SENT_AT.clear()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_message(event: str, lines: Sequence[tuple[str, object]]) -> str:
    """Render an admin DM body from `(label, value)` pairs.

    Example output:

        Event: schedule_paused_invalid_pattern (2026-04-20 12:34:56 UTC)
        - schedule_id: 17
        - reason: pattern type 'bogus' is unsupported

    Truncates to `_TELEGRAM_TEXT_LIMIT` (with an ellipsis) so a chatty
    payload can't be rejected by the Bot API and turn the notification
    itself into a silent error.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    body_lines = [f"Event: {event} ({now_utc} UTC)"]
    for label, value in lines:
        body_lines.append(f"- {label}: {value}")
    msg = "\n".join(body_lines)
    if len(msg) <= _TELEGRAM_TEXT_LIMIT:
        return msg
    return msg[: _TELEGRAM_TEXT_LIMIT - 3] + "..."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def notify_admin(
    bot: ExtBot,
    *,
    event: str,
    lines: Sequence[tuple[str, object]] = (),
    debounce_key: str | None = None,
) -> None:
    """Send a DM to the configured admin user, with debouncing.

    No-op (with a debug log) if `ADMIN_USER_ID` is unset or the same
    `debounce_key` fired within the debounce window. Never raises:
    `send_message` failures are logged and swallowed so that the
    notifier itself cannot bring down the caller.

    Args:
        bot: an `ExtBot` capable of sending DMs.
        event: short snake_case tag identifying the situation; appears
            in the message body and is the default debounce key.
        lines: ordered `(label, value)` diagnostic pairs.
        debounce_key: explicit key for the debounce map. Defaults to
            `event`. Use `f"{event}:{schedule_id}"` for per-schedule
            scoping so two unrelated schedules can both report.
    """
    admin_user_id = _get_admin_user_id()
    if admin_user_id is None:
        logger.debug(
            "notify_admin skipped: ADMIN_USER_ID unset (event=%s)",
            event,
            extra={"event": "notify_admin_skipped_no_admin", "notify_event": event},
        )
        return

    key = debounce_key if debounce_key is not None else event
    if not _should_send(key):
        logger.debug(
            "notify_admin suppressed by debounce (key=%s)",
            key,
            extra={
                "event": "notify_admin_debounced",
                "notify_event": event,
                "debounce_key": key,
            },
        )
        return

    text = format_message(event, lines)
    try:
        await bot.send_message(chat_id=admin_user_id, text=text)
    except Exception:
        logger.exception(
            "Failed to send admin notification (event=%s)",
            event,
            extra={"event": "notify_admin_send_failed", "notify_event": event},
        )
