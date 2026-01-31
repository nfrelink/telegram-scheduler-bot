from __future__ import annotations

from dataclasses import dataclass

import pytest

from database import queries as db
from handlers.forwarding import addforward_command, clearforward_command, forwarding_command, removeforward_command


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
async def test_forwarding_commands_roundtrip(initialized_db) -> None:
    user = _FakeUser(id=3030)

    # Starts empty
    msg0 = _FakeMessage()
    await forwarding_command(_FakeUpdate(message=msg0, effective_user=user), _FakeContext())  # type: ignore[arg-type]
    assert msg0.replies
    assert "allowlist is empty" in msg0.replies[0]["text"].lower()

    # Add
    msg1 = _FakeMessage()
    await addforward_command(
        _FakeUpdate(message=msg1, effective_user=user),
        _FakeContext(args=["-100123"]),
    )  # type: ignore[arg-type]
    assert await db.get_forward_origin_allowlist(user.id) == [-100123]

    # List shows it
    msg2 = _FakeMessage()
    await forwarding_command(_FakeUpdate(message=msg2, effective_user=user), _FakeContext())  # type: ignore[arg-type]
    assert any("-100123" in r["text"] for r in msg2.replies)

    # Remove
    msg3 = _FakeMessage()
    await removeforward_command(
        _FakeUpdate(message=msg3, effective_user=user),
        _FakeContext(args=["-100123"]),
    )  # type: ignore[arg-type]
    assert await db.get_forward_origin_allowlist(user.id) == []

    # Clear is idempotent
    msg4 = _FakeMessage()
    await clearforward_command(_FakeUpdate(message=msg4, effective_user=user), _FakeContext())  # type: ignore[arg-type]
    assert await db.get_forward_origin_allowlist(user.id) == []

