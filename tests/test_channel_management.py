"""Tests for /channels conversation handler (Phase 9e)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from database import queries as db
from handlers.channel_management import (
    _channels_list_text_and_keyboard,
    channels_callback,
)


@dataclass
class _FakeUser:
    id: int
    username: str | None = "u"
    first_name: str | None = "f"
    last_name: str | None = "l"


@dataclass
class _FakeChat:
    id: int = 9000


class _FakeSentMessage:
    message_id = 55


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.replies.append({"text": text, "kwargs": kwargs})
        return _FakeSentMessage()  # type: ignore[return-value]


@dataclass
class _FakeUpdate:
    message: _FakeMessage | None = None
    effective_user: _FakeUser | None = None
    effective_chat: _FakeChat = field(default_factory=_FakeChat)
    callback_query: object | None = None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}
        self.bot = None


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self._answered = False
        self._edited_text: str | None = None
        self._edited_markup = None
        self._alert: str | None = None

    async def answer(self, text: str | None = None, *, show_alert: bool = False, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._answered = True
        if show_alert:
            self._alert = text

    async def edit_message_text(self, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._edited_text = text
        self._edited_markup = kwargs.get("reply_markup")


class _FakeCallbackUpdate:
    def __init__(self, *, data: str, user_id: int) -> None:
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_user = _FakeUser(id=user_id)
        self.message = None


async def _setup_user(user_id: int) -> None:
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)


async def _setup_channel(user_id: int, name: str = "Test Chan") -> dict:
    return await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-100{user_id}",
        channel_name=name,
    )


# ---------------------------------------------------------------------------
# _channels_list_text_and_keyboard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channels_list_empty(initialized_db) -> None:
    user_id = 7001
    await _setup_user(user_id)
    text, keyboard = await _channels_list_text_and_keyboard(user_id)
    assert "no verified channels" in text.lower()
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "ch:add" in buttons


@pytest.mark.asyncio
async def test_channels_list_with_channel(initialized_db) -> None:
    user_id = 7002
    await _setup_user(user_id)
    ch = await _setup_channel(user_id, "My Channel")
    text, keyboard = await _channels_list_text_and_keyboard(user_id)
    assert "1" in text
    ch_id = int(ch["id"])
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert f"ch:rm:{ch_id}" in all_data
    assert "ch:add" in all_data


# ---------------------------------------------------------------------------
# channels_callback — ch:rm (confirmation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channels_callback_rm_shows_confirmation(initialized_db) -> None:
    user_id = 7003
    await _setup_user(user_id)
    ch = await _setup_channel(user_id, "Chan A")
    ch_id = int(ch["id"])

    update = _FakeCallbackUpdate(data=f"ch:rm:{ch_id}", user_id=user_id)
    result = await channels_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert result == 0  # _SHOWING
    assert "remove" in (update.callback_query._edited_text or "").lower()


@pytest.mark.asyncio
async def test_channels_callback_rm_with_cascade_shows_counts(initialized_db) -> None:
    user_id = 7004
    await _setup_user(user_id)
    ch = await _setup_channel(user_id, "Chan B")
    ch_id = int(ch["id"])
    await db.create_schedule(
        channel_db_id=ch_id,
        name="S",
        pattern={"type": "interval", "minutes": 60},
        timezone_name="UTC",
        state="paused",
    )

    update = _FakeCallbackUpdate(data=f"ch:rm:{ch_id}", user_id=user_id)
    await channels_callback(update, _FakeContext())  # type: ignore[arg-type]

    edited = update.callback_query._edited_text or ""
    assert "1 schedule" in edited


# ---------------------------------------------------------------------------
# channels_callback — ch:rmok (actual deletion)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channels_callback_rmok_deletes_channel(initialized_db) -> None:
    user_id = 7005
    await _setup_user(user_id)
    ch = await _setup_channel(user_id, "Chan C")
    ch_id = int(ch["id"])

    update = _FakeCallbackUpdate(data=f"ch:rmok:{ch_id}", user_id=user_id)
    from telegram.ext import ConversationHandler
    result = await channels_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == ConversationHandler.END

    remaining = await db.get_user_channels(user_id)
    assert all(int(c["id"]) != ch_id for c in remaining)


@pytest.mark.asyncio
async def test_channels_callback_rmok_wrong_owner_rejected(initialized_db) -> None:
    user_id = 7006
    other_id = 7007
    await _setup_user(user_id)
    await _setup_user(other_id)
    ch = await _setup_channel(user_id, "Chan D")
    ch_id = int(ch["id"])

    update = _FakeCallbackUpdate(data=f"ch:rmok:{ch_id}", user_id=other_id)
    await channels_callback(update, _FakeContext())  # type: ignore[arg-type]
    # Channel should still exist.
    remaining = await db.get_user_channels(user_id)
    assert any(int(c["id"]) == ch_id for c in remaining)


# ---------------------------------------------------------------------------
# channels_callback — ch:back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channels_callback_back_shows_list(initialized_db) -> None:
    user_id = 7008
    await _setup_user(user_id)
    update = _FakeCallbackUpdate(data="ch:back", user_id=user_id)
    result = await channels_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == 0  # _SHOWING
    assert update.callback_query._edited_text is not None
