from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import error_handler
from services import notifications


def _mock_update() -> MagicMock:
    update = MagicMock()
    update.update_id = 123
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.effective_chat = MagicMock()
    update.effective_chat.id = 777
    update.effective_message = MagicMock()
    update.effective_message.message_id = 999
    return update


@pytest.fixture(autouse=True)
def _clear_notification_state() -> None:
    notifications.reset_debounce_state()


@pytest.mark.asyncio
async def test_error_handler_sends_admin_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "123456")
    update = _mock_update()
    context = MagicMock()
    context.error = RuntimeError("boom")
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()

    await error_handler(update, context)

    context.bot.send_message.assert_awaited_once()
    call = context.bot.send_message.await_args
    assert call.kwargs["chat_id"] == 123456
    text = call.kwargs["text"]
    assert "unhandled_bot_error" in text
    assert "boom" in text
    assert "RuntimeError" in text


@pytest.mark.asyncio
async def test_error_handler_suppresses_duplicate_dm_within_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "123456")
    monkeypatch.setenv("ADMIN_ERROR_DM_DEBOUNCE_SECONDS", "60")
    update = _mock_update()
    context = MagicMock()
    context.error = RuntimeError("boom")
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()

    await error_handler(update, context)
    await error_handler(update, context)

    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_handler_no_admin_config_skips_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    update = _mock_update()
    context = MagicMock()
    context.error = RuntimeError("boom")
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()

    await error_handler(update, context)

    context.bot.send_message.assert_not_called()
