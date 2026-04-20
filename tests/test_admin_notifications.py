"""Tests for `src/services/notifications.py` — env lookups, debouncing,
formatting, and the `notify_admin` happy path / failure-swallowing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services import notifications


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Each test starts from a clean debounce map so cross-test ordering
    can't accidentally suppress a notification."""
    notifications.reset_debounce_state()


def _make_bot() -> MagicMock:
    bot = MagicMock(name="bot")
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# ADMIN_USER_ID gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_admin_no_op_when_admin_user_id_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    bot = _make_bot()

    await notifications.notify_admin(bot, event="anything", lines=[("k", "v")])

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_admin_no_op_when_admin_user_id_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ADMIN_USER_ID is logged-and-ignored, not crashed on."""
    monkeypatch.setenv("ADMIN_USER_ID", "not-a-number")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="anything")

    bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_admin_sends_with_formatted_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    bot = _make_bot()

    await notifications.notify_admin(
        bot,
        event="schedule_paused_invalid_pattern",
        lines=[("schedule_id", 17), ("reason", "bad")],
    )

    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 555
    text = call.kwargs["text"]
    assert text.startswith("Event: schedule_paused_invalid_pattern (")
    assert "- schedule_id: 17" in text
    assert "- reason: bad" in text


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_admin_debounces_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "60")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="evt")
    await notifications.notify_admin(bot, event="evt")

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_admin_distinct_debounce_keys_do_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pause events on different schedules must both fire even when
    they share the same `event` tag."""
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "60")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="evt", debounce_key="evt:1")
    await notifications.notify_admin(bot, event="evt", debounce_key="evt:2")

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_admin_debounce_zero_disables_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "0")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="evt")
    await notifications.notify_admin(bot, event="evt")
    await notifications.notify_admin(bot, event="evt")

    assert bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_notify_admin_debounce_negative_treated_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative env values are clamped to 0 by the env reader; this pins
    that contract so a typo doesn't accidentally re-enable debouncing."""
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "-30")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="evt")
    await notifications.notify_admin(bot, event="evt")

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_admin_invalid_debounce_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "garbage")
    bot = _make_bot()

    await notifications.notify_admin(bot, event="evt")
    await notifications.notify_admin(bot, event="evt")

    bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error tolerance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_admin_swallows_send_message_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If sending the admin DM fails (admin blocked the bot, network blip),
    the caller must not see the exception. Caller flows are real user
    operations and shouldn't be killed by a failure to *report* the
    failure."""
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("blocked"))

    await notifications.notify_admin(bot, event="evt")

    bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_message_truncates_to_telegram_limit() -> None:
    """A chatty payload must be truncated to 4096 chars so the Bot API
    doesn't reject the notification itself."""
    huge_value = "x" * 8000
    msg = notifications.format_message(
        "evt", [("k1", huge_value), ("k2", "v2")]
    )
    assert len(msg) == 4096
    assert msg.endswith("...")


def test_format_message_renders_label_value_pairs_in_order() -> None:
    """Stable ordering matters: tests that pin the expected payload
    shouldn't be flaky based on dict iteration order."""
    msg = notifications.format_message(
        "evt", [("first", 1), ("second", 2), ("third", 3)]
    )
    body = msg.splitlines()
    assert body[0].startswith("Event: evt (")
    assert body[1] == "- first: 1"
    assert body[2] == "- second: 2"
    assert body[3] == "- third: 3"


def test_format_message_with_no_lines_renders_only_header() -> None:
    msg = notifications.format_message("evt", [])
    assert msg.startswith("Event: evt (")
    assert "\n" not in msg
