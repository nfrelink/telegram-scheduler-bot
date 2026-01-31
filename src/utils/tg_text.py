"""Small helpers for building Telegram messages with entities.

We prefer entities over Markdown/HTML parse modes to avoid escaping issues.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import MessageEntity


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units (Telegram entity offsets use this)."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Segment:
    text: str
    code: bool = False


def render(segments: list[Segment]) -> tuple[str, list[MessageEntity] | None]:
    """Render segments into text + entities list (or None)."""
    parts: list[str] = []
    entities: list[MessageEntity] = []
    offset = 0

    for seg in segments:
        parts.append(seg.text)
        seg_len = utf16_len(seg.text)
        if seg.code and seg_len:
            entities.append(MessageEntity(type="code", offset=offset, length=seg_len))
        offset += seg_len

    return "".join(parts), (entities or None)

