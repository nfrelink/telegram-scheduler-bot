"""Tests for /forward conversation handler (Phase 9d)."""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from database import queries as db
from handlers.forwarding import (
    _list_text_and_keyboard,
    forward_add_handler,
    forward_callback,
    forward_command,
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


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[dict] = []
        self.forward_from_chat = None
        self.forward_origin = None
        self.message_id = 42

    async def reply_text(self, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.replies.append({"text": text, "kwargs": kwargs})

    async def delete(self) -> None:
        pass


@dataclass
class _FakeUpdate:
    message: _FakeMessage | None = None
    effective_user: _FakeUser | None = None
    effective_chat: _FakeChat = field(default_factory=_FakeChat)
    callback_query: object | None = None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}
        self.bot = AsyncMock()


class _FakeSentMessage:
    message_id = 42


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self._answered = False
        self._edited_text: str | None = None
        self._edited_markup = None

    async def answer(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._answered = True

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


# ---------------------------------------------------------------------------
# _list_text_and_keyboard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_empty(initialized_db) -> None:
    user_id = 6001
    await _setup_user(user_id)
    text, keyboard = await _list_text_and_keyboard(user_id)
    assert "empty" in text.lower()
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "fw:add" in buttons


@pytest.mark.asyncio
async def test_list_with_entries(initialized_db) -> None:
    user_id = 6002
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100111)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100222)
    text, keyboard = await _list_text_and_keyboard(user_id)
    assert "2 channels" in text.lower()
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "fw:rm:-100111" in all_data
    assert "fw:rm:-100222" in all_data
    assert "fw:add" in all_data
    assert "fw:clear" in all_data


@pytest.mark.asyncio
async def test_list_shows_stored_name(initialized_db) -> None:
    """When a name is stored the button label shows the name, not the raw ID."""
    user_id = 6012
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(
        user_id=user_id, origin_chat_id=-100333, origin_channel_name="Friend Channel"
    )
    _, keyboard = await _list_text_and_keyboard(user_id)
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Friend Channel" in labels
    assert "-100333" not in labels


@pytest.mark.asyncio
async def test_list_falls_back_to_id_when_no_name(initialized_db) -> None:
    """Without a stored name the button falls back to the raw numeric ID."""
    user_id = 6013
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100444)
    _, keyboard = await _list_text_and_keyboard(user_id)
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "-100444" in labels


# ---------------------------------------------------------------------------
# forward_command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_command_sends_list(initialized_db) -> None:
    user_id = 6003
    await _setup_user(user_id)

    msg = _FakeMessage()
    msg.replies = []

    async def fake_reply(text, **kwargs):
        msg.replies.append({"text": text, "kwargs": kwargs})
        return _FakeSentMessage()

    msg.reply_text = fake_reply  # type: ignore[method-assign]

    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    result = await forward_command(update, ctx)  # type: ignore[arg-type]

    assert msg.replies
    assert result == 0  # _SHOWING
    assert ctx.user_data.get("fw_msg_id") == 42


# ---------------------------------------------------------------------------
# forward_callback — add trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_callback_add_returns_awaiting_add(initialized_db) -> None:
    user_id = 6004
    await _setup_user(user_id)
    update = _FakeCallbackUpdate(data="fw:add", user_id=user_id)
    result = await forward_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == 1  # _AWAITING_ADD
    assert update.callback_query._answered


# ---------------------------------------------------------------------------
# forward_callback — remove entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_callback_remove_deletes_entry(initialized_db) -> None:
    user_id = 6005
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100555)

    update = _FakeCallbackUpdate(data="fw:rm:-100555", user_id=user_id)
    from telegram.ext import ConversationHandler
    result = await forward_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    assert await db.get_forward_origin_allowlist(user_id) == []


# ---------------------------------------------------------------------------
# forward_callback — clear confirmation and clearok
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_callback_clear_shows_confirmation(initialized_db) -> None:
    user_id = 6006
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100777)

    update = _FakeCallbackUpdate(data="fw:clear", user_id=user_id)
    result = await forward_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == 0  # _SHOWING
    assert "remove all" in (update.callback_query._edited_text or "").lower()


@pytest.mark.asyncio
async def test_forward_callback_clearok_removes_all(initialized_db) -> None:
    user_id = 6007
    await _setup_user(user_id)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100888)
    await db.add_forward_origin_allowlist(user_id=user_id, origin_chat_id=-100999)

    update = _FakeCallbackUpdate(data="fw:clearok", user_id=user_id)
    from telegram.ext import ConversationHandler
    result = await forward_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    assert await db.get_forward_origin_allowlist(user_id) == []


# ---------------------------------------------------------------------------
# forward_callback — back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_callback_back_shows_list(initialized_db) -> None:
    user_id = 6008
    await _setup_user(user_id)
    update = _FakeCallbackUpdate(data="fw:back", user_id=user_id)
    result = await forward_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == 0  # _SHOWING


# ---------------------------------------------------------------------------
# forward_add_handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_add_handler_text_channel_id(initialized_db) -> None:
    user_id = 6009
    await _setup_user(user_id)

    msg = _FakeMessage(text="-100321")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    ctx.bot.get_chat = AsyncMock(side_effect=Exception("unavailable"))

    from telegram.ext import ConversationHandler
    result = await forward_add_handler(update, ctx)  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    assert -100321 in await db.get_forward_origin_allowlist(user_id)


@pytest.mark.asyncio
async def test_forward_add_handler_invalid_text_stays_in_state(initialized_db) -> None:
    user_id = 6010
    await _setup_user(user_id)

    msg = _FakeMessage(text="not a number")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))

    result = await forward_add_handler(update, _FakeContext())  # type: ignore[arg-type]
    assert result == 1  # _AWAITING_ADD
    assert msg.replies
    assert "channel id" in msg.replies[0]["text"].lower()


@pytest.mark.asyncio
async def test_forward_add_handler_forwarded_message(initialized_db) -> None:
    user_id = 6011
    await _setup_user(user_id)

    class _FakeForwardChat:
        id = -100654
        title = "Some Friend Channel"

    msg = _FakeMessage()
    msg.forward_from_chat = _FakeForwardChat()
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))

    from telegram.ext import ConversationHandler
    result = await forward_add_handler(update, _FakeContext())  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    assert -100654 in await db.get_forward_origin_allowlist(user_id)
    with_names = await db.get_forward_origin_allowlist_with_names(user_id)
    assert any(cid == -100654 and name == "Some Friend Channel" for cid, name in with_names)


@pytest.mark.asyncio
async def test_forward_add_handler_text_id_stores_name_from_get_chat(initialized_db) -> None:
    """When the user types a numeric ID, get_chat() is called and the title is stored."""
    user_id = 6014
    await _setup_user(user_id)

    class _FakeChat:
        title = "Resolved Channel"

    ctx = _FakeContext()
    ctx.bot.get_chat = AsyncMock(return_value=_FakeChat())

    msg = _FakeMessage(text="-100789")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))

    from telegram.ext import ConversationHandler
    result = await forward_add_handler(update, ctx)  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    with_names = await db.get_forward_origin_allowlist_with_names(user_id)
    assert any(cid == -100789 and name == "Resolved Channel" for cid, name in with_names)


@pytest.mark.asyncio
async def test_forward_add_handler_text_id_falls_back_on_get_chat_failure(initialized_db) -> None:
    """If get_chat() fails (private channel the bot can't see), entry is still stored."""
    user_id = 6015
    await _setup_user(user_id)

    ctx = _FakeContext()
    ctx.bot.get_chat = AsyncMock(side_effect=Exception("Forbidden"))

    msg = _FakeMessage(text="-100999")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))

    from telegram.ext import ConversationHandler
    result = await forward_add_handler(update, ctx)  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    assert -100999 in await db.get_forward_origin_allowlist(user_id)
    with_names = await db.get_forward_origin_allowlist_with_names(user_id)
    assert any(cid == -100999 and name is None for cid, name in with_names)
