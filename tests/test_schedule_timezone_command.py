from __future__ import annotations

from dataclasses import dataclass

import pytest

from database import queries as db
from handlers.schedule_management import setscheduletimezone_command


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


class _FakeContext:
    def __init__(self, *, args: list[str] | None = None) -> None:
        self.args = args or []
        self.user_data: dict = {}


@pytest.mark.asyncio
async def test_setscheduletimezone_updates_selected_schedule(initialized_db) -> None:
    user = _FakeUser(id=202)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-20202", channel_name="C")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 1},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    # Select the schedule, then set timezone with a single arg.
    await db.set_user_context(user_id=user.id, selected_channel_id=int(channel["id"]), selected_schedule_id=schedule_id)

    msg = _FakeMessage()
    upd = _FakeUpdate(message=msg, effective_user=user)
    ctx = _FakeContext(args=["Europe/Amsterdam"])

    await setscheduletimezone_command(upd, ctx)  # type: ignore[arg-type]
    assert msg.replies

    updated = await db.get_schedule_for_user(user.id, schedule_id)
    assert updated is not None
    assert updated["timezone"] == "Europe/Amsterdam"


@pytest.mark.asyncio
async def test_setscheduletimezone_updates_by_explicit_id(initialized_db) -> None:
    user = _FakeUser(id=203)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-20303", channel_name="C")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 1},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    msg = _FakeMessage()
    upd = _FakeUpdate(message=msg, effective_user=user)
    ctx = _FakeContext(args=[str(schedule_id), "UTC"])

    await setscheduletimezone_command(upd, ctx)  # type: ignore[arg-type]
    assert msg.replies

    updated = await db.get_schedule_for_user(user.id, schedule_id)
    assert updated is not None
    assert updated["timezone"] == "UTC"

