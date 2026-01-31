from __future__ import annotations

import pytest
from telegram.constants import ChatType

from handlers.channel_info import channelid_command


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, *, chat_id: int, text: str, entities=None, **_kwargs):  # type: ignore[no-untyped-def]
        self.sent.append({"chat_id": chat_id, "text": text, "entities": entities})


class _FakeContext:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


class _FakeChat:
    def __init__(self, *, chat_id: int, title: str, username: str | None = None) -> None:
        self.id = chat_id
        self.type = ChatType.CHANNEL
        self.title = title
        self.username = username


class _FakeUpdate:
    def __init__(self, *, chat: _FakeChat) -> None:
        self.effective_chat = chat
        self.effective_message = None


@pytest.mark.asyncio
async def test_channelid_posts_channel_id_to_channel() -> None:
    bot = _FakeBot()
    context = _FakeContext(bot)
    chat = _FakeChat(chat_id=-100123, title="Test Channel", username="testchannel")
    update = _FakeUpdate(chat=chat)

    await channelid_command(update, context)  # type: ignore[arg-type]
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == -100123
    assert "-100123" in sent["text"]
    assert "/addchannel" in sent["text"]

