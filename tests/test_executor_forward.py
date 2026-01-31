from __future__ import annotations

import pytest
from telegram.error import BadRequest

from scheduler import executor


class _FakeBot:
    def __init__(self) -> None:
        self.forward_calls: list[tuple[str, int, int]] = []
        self.forward_messages_calls: list[tuple[str, int, list[int]]] = []
        self.should_fail = False

    async def forward_message(self, *, chat_id: str, from_chat_id: int, message_id: int, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        if self.should_fail:
            raise BadRequest("Bad Request: message to forward not found")
        self.forward_calls.append((chat_id, int(from_chat_id), int(message_id)))

    async def forward_messages(  # type: ignore[no-untyped-def]
        self, chat_id: str, from_chat_id: int, message_ids: list[int], **_kwargs
    ):
        if self.should_fail:
            raise BadRequest("Bad Request: message to forward not found")
        self.forward_messages_calls.append((chat_id, int(from_chat_id), list(message_ids)))
        # Only length matters for our assertions.
        return tuple(range(len(message_ids)))


@pytest.mark.asyncio
async def test_send_post_forwards_when_forward_metadata_present(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_send_once(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("_send_post_once should not be called when forwarding succeeds")

    monkeypatch.setattr(executor, "_send_post_once", fail_send_once)

    bot = _FakeBot()
    post = {
        "id": 1,
        "media_type": "photo",
        "file_id": "abc",
        "file_path": None,
        "caption": None,
        "forward_from_chat_id": 111,
        "forward_from_message_id": 42,
    }

    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is True
    assert bot.forward_calls == [("-1", 111, 42)]


@pytest.mark.asyncio
async def test_send_post_returns_false_if_forward_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_send_once(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        raise AssertionError("_send_post_once should not be called when forwarding fails")

    monkeypatch.setattr(executor, "_send_post_once", fail_send_once)

    bot = _FakeBot()
    bot.should_fail = True
    post = {
        "id": 2,
        "media_type": "photo",
        "file_id": "abc",
        "file_path": None,
        "caption": None,
        "forward_from_chat_id": 111,
        "forward_from_message_id": 99,
    }

    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is False


@pytest.mark.asyncio
async def test_send_post_forwards_media_group_when_media_group_data_has_forward_refs() -> None:
    bot = _FakeBot()
    post = {
        "id": 3,
        "media_type": "media_group",
        "file_id": None,
        "file_path": None,
        "caption": None,
        "media_group_data": """
        [
          {"media_type":"photo","file_id":"x","file_path":null,"caption":null,"caption_parse_mode":null,"caption_entities":null,
           "forward_from_chat_id":111,"forward_from_message_id":43},
          {"media_type":"photo","file_id":"y","file_path":null,"caption":null,"caption_parse_mode":null,"caption_entities":null,
           "forward_from_chat_id":111,"forward_from_message_id":42}
        ]
        """,
    }

    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is True
    assert bot.forward_messages_calls == [("-1", 111, [42, 43])]


@pytest.mark.asyncio
async def test_send_post_returns_false_if_media_group_forward_fails() -> None:
    bot = _FakeBot()
    bot.should_fail = True
    post = {
        "id": 4,
        "media_type": "media_group",
        "file_id": None,
        "file_path": None,
        "caption": None,
        "media_group_data": """
        [
          {"media_type":"photo","file_id":"x","file_path":null,"caption":null,"caption_parse_mode":null,"caption_entities":null,
           "forward_from_chat_id":111,"forward_from_message_id":42}
        ]
        """,
    }

    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is False

