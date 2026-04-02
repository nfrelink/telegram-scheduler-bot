"""Tests for /schedules ConversationHandler (Phase 9f)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from database import queries as db
from handlers.schedule_management import (
    SM_SHOWING,
    SM_WAIT_TZ_INPUT,
    NS_WAIT_NAME,
    NS_WAIT_TYPE,
    ES_WAIT_FIELD,
    ES_WAIT_NAME,
    _schedules_list_text_and_keyboard,
    schedules_callback,
    schedules_tz_handler,
    newschedule_set_name,
    newschedule_set_type,
    editschedule_choose_field,
    editschedule_set_name,
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
    message_id = 77


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
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


async def _setup_channel_and_schedule(user_id: int) -> tuple[dict, dict]:
    ch = await db.create_channel(user_id=user_id, telegram_channel_id=f"-100{user_id}", channel_name="Test Chan")
    s = await db.create_schedule(
        channel_db_id=int(ch["id"]),
        name="Test Sched",
        pattern={"type": "interval", "minutes": 60},
        timezone_name="UTC",
        state="paused",
    )
    return ch, s


# ---------------------------------------------------------------------------
# _schedules_list_text_and_keyboard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_list_no_channel_selected(initialized_db) -> None:
    user_id = 8001
    await _setup_user(user_id)
    text, keyboard = await _schedules_list_text_and_keyboard(user_id)
    assert "no channel selected" in text.lower()
    assert keyboard is None


@pytest.mark.asyncio
async def test_schedules_list_with_schedule(initialized_db) -> None:
    user_id = 8002
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=int(s["id"]))
    text, keyboard = await _schedules_list_text_and_keyboard(user_id)
    assert "Test Chan" in text
    assert keyboard is not None
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert f"sm:rm:{s['id']}" in all_data
    assert f"sm:edit:{s['id']}" in all_data
    assert "sm:new" in all_data


# ---------------------------------------------------------------------------
# schedules_callback — pause / resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_callback_pause(initialized_db) -> None:
    user_id = 8003
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=int(s["id"]))
    await db.update_schedule_state(int(s["id"]), "active")

    update = _FakeCallbackUpdate(data=f"sm:pause:{s['id']}", user_id=user_id)
    result = await schedules_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == SM_SHOWING
    refreshed = await db.get_schedule_for_user(user_id, int(s["id"]))
    assert refreshed["state"] == "paused"


@pytest.mark.asyncio
async def test_schedules_callback_resume_empty_queue_blocked(initialized_db) -> None:
    user_id = 8004
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=int(s["id"]))

    update = _FakeCallbackUpdate(data=f"sm:resume:{s['id']}", user_id=user_id)
    result = await schedules_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == SM_SHOWING
    # State unchanged because queue is empty.
    refreshed = await db.get_schedule_for_user(user_id, int(s["id"]))
    assert refreshed["state"] == "paused"
    assert update.callback_query._alert is not None


# ---------------------------------------------------------------------------
# schedules_callback — delete confirmation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_callback_rm_shows_confirmation(initialized_db) -> None:
    user_id = 8005
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=int(s["id"]))

    update = _FakeCallbackUpdate(data=f"sm:rm:{s['id']}", user_id=user_id)
    result = await schedules_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == SM_SHOWING
    assert "delete" in (update.callback_query._edited_text or "").lower()


@pytest.mark.asyncio
async def test_schedules_callback_rmok_deletes(initialized_db) -> None:
    user_id = 8006
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    s_id = int(s["id"])
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=s_id)

    update = _FakeCallbackUpdate(data=f"sm:rmok:{s_id}", user_id=user_id)
    result = await schedules_callback(update, _FakeContext())  # type: ignore[arg-type]
    assert result == SM_SHOWING  # stays showing (refreshed list)
    deleted = await db.get_schedule_for_user(user_id, s_id)
    assert deleted is None


# ---------------------------------------------------------------------------
# schedules_callback — sm:new transitions to NS_WAIT_NAME
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_callback_new_transitions_to_ns(initialized_db) -> None:
    user_id = 8007
    await _setup_user(user_id)
    ch, _ = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=None)

    update = _FakeCallbackUpdate(data="sm:new", user_id=user_id)
    ctx = _FakeContext()
    result = await schedules_callback(update, ctx)  # type: ignore[arg-type]
    assert result == NS_WAIT_NAME
    assert ctx.user_data.get("ns_channel_db_id") == int(ch["id"])


# ---------------------------------------------------------------------------
# schedules_callback — sm:edit transitions to ES_WAIT_FIELD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_callback_edit_transitions_to_es(initialized_db) -> None:
    user_id = 8008
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    await db.set_user_context(user_id=user_id, selected_channel_id=int(ch["id"]), selected_schedule_id=int(s["id"]))

    update = _FakeCallbackUpdate(data=f"sm:edit:{s['id']}", user_id=user_id)
    ctx = _FakeContext()
    result = await schedules_callback(update, ctx)  # type: ignore[arg-type]
    assert result == ES_WAIT_FIELD
    assert ctx.user_data.get("es_schedule_id") == int(s["id"])


# ---------------------------------------------------------------------------
# schedules_tz_handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedules_tz_handler_valid(initialized_db) -> None:
    user_id = 8009
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    s_id = int(s["id"])

    msg = _FakeMessage("Europe/Amsterdam")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    ctx.user_data["sm_settp_schedule_id"] = s_id

    from telegram.ext import ConversationHandler
    result = await schedules_tz_handler(update, ctx)  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    refreshed = await db.get_schedule_for_user(user_id, s_id)
    assert refreshed["timezone"] == "Europe/Amsterdam"


@pytest.mark.asyncio
async def test_schedules_tz_handler_invalid_stays(initialized_db) -> None:
    user_id = 8010
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)

    msg = _FakeMessage("Not/ATimezone")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    ctx.user_data["sm_settp_schedule_id"] = int(s["id"])

    result = await schedules_tz_handler(update, ctx)  # type: ignore[arg-type]
    assert result == SM_WAIT_TZ_INPUT
    assert msg.replies


# ---------------------------------------------------------------------------
# Embedded new-schedule wizard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_newschedule_set_name_stores_and_advances(initialized_db) -> None:
    user_id = 8011
    await _setup_user(user_id)

    msg = _FakeMessage("My Schedule")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()

    result = await newschedule_set_name(update, ctx)  # type: ignore[arg-type]
    assert result == NS_WAIT_TYPE
    assert ctx.user_data["ns_name"] == "My Schedule"


@pytest.mark.asyncio
async def test_newschedule_set_type_interval_advances(initialized_db) -> None:
    user_id = 8012
    await _setup_user(user_id)

    msg = _FakeMessage("interval")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    ctx.user_data["ns_timezone"] = "UTC"

    from handlers.schedule_management import NS_WAIT_INTERVAL
    result = await newschedule_set_type(update, ctx)  # type: ignore[arg-type]
    assert result == NS_WAIT_INTERVAL


# ---------------------------------------------------------------------------
# Embedded edit-schedule wizard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_editschedule_choose_field_name(initialized_db) -> None:
    user_id = 8013
    await _setup_user(user_id)

    msg = _FakeMessage("name")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))

    result = await editschedule_choose_field(update, _FakeContext())  # type: ignore[arg-type]
    assert result == ES_WAIT_NAME


@pytest.mark.asyncio
async def test_editschedule_set_name_updates_db(initialized_db) -> None:
    user_id = 8014
    await _setup_user(user_id)
    ch, s = await _setup_channel_and_schedule(user_id)
    s_id = int(s["id"])

    msg = _FakeMessage("New Name")
    update = _FakeUpdate(message=msg, effective_user=_FakeUser(id=user_id))
    ctx = _FakeContext()
    ctx.user_data["es_schedule_id"] = s_id

    from telegram.ext import ConversationHandler
    result = await editschedule_set_name(update, ctx)  # type: ignore[arg-type]
    assert result == ConversationHandler.END
    refreshed = await db.get_schedule_for_user(user_id, s_id)
    assert refreshed["name"] == "New Name"
