from __future__ import annotations

import pytest
from telegram import MessageEntity

from handlers import admin


class _FakeMessage:
    def __init__(self, *, text: str, entities: list[MessageEntity] | None = None) -> None:
        self.text = text
        self.entities = entities or []


def test_extract_command_payload_strips_prefix_and_shifts_entities() -> None:
    # "/broadcast " is 11 chars in UTF-16 (ASCII)
    msg = _FakeMessage(
        text="/broadcast hi",
        entities=[
            MessageEntity(type="bot_command", offset=0, length=10),
            MessageEntity(type="code", offset=11, length=2),
        ],
    )

    payload_text, payload_entities = admin._extract_command_payload(msg)  # type: ignore[attr-defined]
    assert payload_text == "hi"
    assert payload_entities is not None
    assert len(payload_entities) == 1
    assert payload_entities[0].type == "code"
    assert payload_entities[0].offset == 0
    assert payload_entities[0].length == 2


def test_extract_command_payload_none_when_missing_message() -> None:
    msg = _FakeMessage(text="/broadcast", entities=[MessageEntity(type="bot_command", offset=0, length=10)])
    payload_text, payload_entities = admin._extract_command_payload(msg)  # type: ignore[attr-defined]
    assert payload_text is None
    assert payload_entities is None

