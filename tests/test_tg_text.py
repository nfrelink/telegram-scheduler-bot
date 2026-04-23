"""Tests for src/utils/tg_text.py — utf16_len and render."""

from __future__ import annotations

from telegram import MessageEntity

from utils.tg_text import Segment, render, utf16_len


# ---------------------------------------------------------------------------
# utf16_len
# ---------------------------------------------------------------------------


def test_utf16_len_empty() -> None:
    assert utf16_len("") == 0


def test_utf16_len_ascii() -> None:
    # Each ASCII character is exactly 1 UTF-16 code unit.
    assert utf16_len("hello") == 5


def test_utf16_len_bmp_char_is_one_code_unit() -> None:
    # U+00E9 (é) is in the Basic Multilingual Plane: 1 code unit.
    assert utf16_len("é") == 1


def test_utf16_len_emoji_is_two_code_units() -> None:
    # U+1F600 (😀) is a supplementary character: 2 UTF-16 code units.
    assert utf16_len("😀") == 2


def test_utf16_len_mixed_string() -> None:
    # "Hi😀" = 2 (ASCII) + 2 (emoji) = 4 code units.
    assert utf16_len("Hi😀") == 4


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_empty_segments() -> None:
    text, entities = render([])
    assert text == ""
    assert entities is None


def test_render_plain_segments_only_no_entities() -> None:
    text, entities = render([Segment("Hello"), Segment(", world!")])
    assert text == "Hello, world!"
    assert entities is None


def test_render_single_code_segment_produces_entity() -> None:
    text, entities = render([Segment("foo", code=True)])
    assert text == "foo"
    assert entities is not None
    assert len(entities) == 1
    ent = entities[0]
    assert ent.type == MessageEntity.CODE
    assert ent.offset == 0
    assert ent.length == 3


def test_render_mixed_entity_offset_follows_preceding_text() -> None:
    # "abc" (plain, 3 code units) followed by "xyz" (code, 3 code units).
    # The entity for "xyz" must start at offset 3.
    text, entities = render([Segment("abc"), Segment("xyz", code=True)])
    assert text == "abcxyz"
    assert entities is not None
    assert len(entities) == 1
    assert entities[0].offset == 3
    assert entities[0].length == 3


def test_render_emoji_in_plain_shifts_code_offset_correctly() -> None:
    # "😀" is 2 UTF-16 code units, so the code entity must start at offset 2.
    text, entities = render([Segment("😀"), Segment("X", code=True)])
    assert text == "😀X"
    assert entities is not None
    assert entities[0].offset == 2
    assert entities[0].length == 1


def test_render_empty_code_segment_emits_no_entity() -> None:
    # An empty code segment has length 0 and the guard `if seg_len` skips it.
    text, entities = render([Segment("", code=True)])
    assert text == ""
    assert entities is None


def test_render_multiple_code_segments_each_get_entity() -> None:
    segments = [
        Segment("a", code=True),
        Segment(" "),
        Segment("b", code=True),
    ]
    text, entities = render(segments)
    assert text == "a b"
    assert entities is not None
    assert len(entities) == 2
    assert entities[0].offset == 0
    assert entities[0].length == 1
    assert entities[1].offset == 2
    assert entities[1].length == 1
