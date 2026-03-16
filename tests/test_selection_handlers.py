"""Tests for /select command and callback (Phase 9b)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from database import queries as db
from handlers.selection import select_callback, select_command, selection_segments


@dataclass
class _FakeUser:
    id: int
    username: str | None = "u"
    first_name: str | None = "f"
    last_name: str | None = "l"


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.replies.append({"text": text, "kwargs": kwargs})


@dataclass
class _FakeUpdate:
    message: _FakeMessage
    effective_user: _FakeUser | None = None
    effective_chat: object | None = None
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


async def _setup_channel_and_schedule(user_id: int) -> tuple[dict, dict]:
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)
    channel = await db.create_channel(user_id=user_id, telegram_channel_id=f"-100{user_id}", channel_name="Test Chan")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Test Sched",
        pattern={"type": "interval", "minutes": 60},
        timezone_name="UTC",
        state="paused",
    )
    return channel, schedule


# ---------------------------------------------------------------------------
# selection_segments
# ---------------------------------------------------------------------------

def test_selection_segments_no_selection() -> None:
    segments = selection_segments({})
    text = "".join(s.text for s in segments)
    assert "none" in text.lower()


def test_selection_segments_with_channel_and_schedule() -> None:
    details = {
        "telegram_channel_id": "-10099",
        "channel_name": "My Chan",
        "selected_schedule_id": 7,
        "schedule_name": "Daily",
        "schedule_state": "active",
    }
    segments = selection_segments(details)
    text = "".join(s.text for s in segments)
    assert "My Chan" in text
    assert "Daily" in text


# ---------------------------------------------------------------------------
# select_command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_command_shows_channels_keyboard(initialized_db) -> None:
    user_id = 5001
    channel, _ = await _setup_channel_and_schedule(user_id)

    msg = _FakeMessage()
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    await select_command(update, _FakeContext())  # type: ignore[arg-type]

    assert msg.replies
    reply = msg.replies[0]
    assert "channel" in reply["text"].lower()
    assert reply["kwargs"].get("reply_markup") is not None


@pytest.mark.asyncio
async def test_select_command_no_channels_sends_text(initialized_db) -> None:
    user_id = 5002
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    msg = _FakeMessage()
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    await select_command(update, _FakeContext())  # type: ignore[arg-type]

    assert msg.replies
    assert "no verified channels" in msg.replies[0]["text"].lower()


# ---------------------------------------------------------------------------
# select_callback — channel drill-in (sc:ch:)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_callback_channel_shows_schedules(initialized_db) -> None:
    user_id = 5003
    channel, _ = await _setup_channel_and_schedule(user_id)
    channel_id = int(channel["id"])

    update = _FakeCallbackUpdate(data=f"sc:ch:{channel_id}", user_id=user_id)
    await select_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert update.callback_query._edited_text is not None
    assert "schedule" in update.callback_query._edited_text.lower()
    assert update.callback_query._edited_markup is not None


@pytest.mark.asyncio
async def test_select_callback_channel_wrong_owner_rejected(initialized_db) -> None:
    user_id = 5004
    other_user_id = 5005
    channel, _ = await _setup_channel_and_schedule(user_id)
    await db.upsert_user(user_id=other_user_id, username="u", first_name="f", last_name="l", is_admin=False)

    channel_id = int(channel["id"])
    update = _FakeCallbackUpdate(data=f"sc:ch:{channel_id}", user_id=other_user_id)
    await select_callback(update, _FakeContext())  # type: ignore[arg-type]

    # Should answer with an alert, not edit the message.
    assert update.callback_query._answered
    assert update.callback_query._edited_text is None


# ---------------------------------------------------------------------------
# select_callback — set selection (sc:set:)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_callback_set_stores_selection_and_confirms(initialized_db) -> None:
    user_id = 5006
    channel, schedule = await _setup_channel_and_schedule(user_id)
    channel_id = int(channel["id"])
    schedule_id = int(schedule["id"])

    update = _FakeCallbackUpdate(data=f"sc:set:{channel_id}:{schedule_id}", user_id=user_id)
    await select_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "Test Chan" in (update.callback_query._edited_text or "")
    assert "Test Sched" in (update.callback_query._edited_text or "")

    ctx = await db.get_user_context(user_id)
    assert int(ctx["selected_channel_id"]) == channel_id
    assert int(ctx["selected_schedule_id"]) == schedule_id


@pytest.mark.asyncio
async def test_select_callback_set_wrong_owner_rejected(initialized_db) -> None:
    user_id = 5007
    other_user_id = 5008
    channel, schedule = await _setup_channel_and_schedule(user_id)
    await db.upsert_user(user_id=other_user_id, username="u", first_name="f", last_name="l", is_admin=False)

    channel_id = int(channel["id"])
    schedule_id = int(schedule["id"])
    update = _FakeCallbackUpdate(data=f"sc:set:{channel_id}:{schedule_id}", user_id=other_user_id)
    await select_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert update.callback_query._edited_text is None


# ---------------------------------------------------------------------------
# select_callback — back to channel list (sc:back)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_callback_back_shows_channel_list(initialized_db) -> None:
    user_id = 5009
    await _setup_channel_and_schedule(user_id)

    update = _FakeCallbackUpdate(data="sc:back", user_id=user_id)
    await select_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "channel" in (update.callback_query._edited_text or "").lower()
