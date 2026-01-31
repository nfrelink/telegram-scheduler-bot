from __future__ import annotations

from dataclasses import dataclass

import pytest

from database import queries as db
from handlers.selection import selectschedule_command, selection_command


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
        self.bot = None


@pytest.mark.asyncio
async def test_selectschedule_sets_context_and_selection_command_shows_it(initialized_db) -> None:
    user = _FakeUser(id=101)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-10101", channel_name="C")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="S",
        pattern={"type": "interval", "minutes": 5},
        timezone_name="UTC",
        state="paused",
    )

    msg1 = _FakeMessage()
    upd1 = _FakeUpdate(message=msg1, effective_user=user)
    ctx1 = _FakeContext(args=[str(schedule["id"])])

    await selectschedule_command(upd1, ctx1)  # type: ignore[arg-type]
    details = await db.get_user_context_details(user.id)
    assert int(details["selected_schedule_id"]) == int(schedule["id"])

    msg2 = _FakeMessage()
    upd2 = _FakeUpdate(message=msg2, effective_user=user)
    ctx2 = _FakeContext()

    await selection_command(upd2, ctx2)  # type: ignore[arg-type]
    assert msg2.replies
    assert "Current selection" in msg2.replies[0]["text"]

