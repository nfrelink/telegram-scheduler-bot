"""Tests for Phase 6: album viewer in the queue browser.

Covers _all_media_from_group — the helper that builds an InputMedia list from
the JSON-encoded media_group_data stored on a queued post.
"""

from __future__ import annotations

import json

import pytest
from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from handlers.queue_management import _all_media_from_group


def _post(items: list[dict]) -> dict:
    """Build a minimal queued-post dict with serialised media_group_data."""
    return {"media_group_data": json.dumps(items)}


def test_all_media_from_group_extracts_photo_and_video() -> None:
    post = _post([
        {"file_id": "fid_photo", "media_type": "photo", "caption": "cap1", "caption_entities": None},
        {"file_id": "fid_video", "media_type": "video", "caption": None, "caption_entities": None},
    ])
    result = _all_media_from_group(post)
    assert len(result) == 2
    assert isinstance(result[0], InputMediaPhoto)
    assert result[0].media == "fid_photo"
    assert result[0].caption == "cap1"
    assert isinstance(result[1], InputMediaVideo)
    assert result[1].media == "fid_video"
    assert result[1].caption is None


def test_all_media_from_group_falls_back_to_document() -> None:
    post = _post([
        {"file_id": "fid_doc", "media_type": "document", "caption": None, "caption_entities": None},
    ])
    result = _all_media_from_group(post)
    assert len(result) == 1
    assert isinstance(result[0], InputMediaDocument)
    assert result[0].media == "fid_doc"


def test_all_media_from_group_skips_items_without_file_id() -> None:
    post = _post([
        {"file_id": "fid_ok", "media_type": "photo", "caption": None, "caption_entities": None},
        {"file_id": None, "media_type": "photo", "caption": None, "caption_entities": None},
        {"media_type": "photo", "caption": None, "caption_entities": None},  # missing key
    ])
    result = _all_media_from_group(post)
    assert len(result) == 1
    assert result[0].media == "fid_ok"


def test_all_media_from_group_skips_unknown_media_type() -> None:
    post = _post([
        {"file_id": "fid_good", "media_type": "photo", "caption": None, "caption_entities": None},
        {"file_id": "fid_bad", "media_type": "sticker", "caption": None, "caption_entities": None},
    ])
    result = _all_media_from_group(post)
    assert len(result) == 1
    assert result[0].media == "fid_good"


def test_all_media_from_group_returns_empty_on_missing_data() -> None:
    assert _all_media_from_group({}) == []
    assert _all_media_from_group({"media_group_data": None}) == []
    assert _all_media_from_group({"media_group_data": "[]"}) == []


def test_all_media_from_group_returns_empty_on_bad_json() -> None:
    post = {"media_group_data": "not valid json {{{"}
    assert _all_media_from_group(post) == []


def test_all_media_from_group_passes_caption_entities() -> None:
    entities = [{"type": "bold", "offset": 0, "length": 3}]
    post = _post([
        {
            "file_id": "fid_ent",
            "media_type": "photo",
            "caption": "cap",
            "caption_entities": json.dumps(entities),
        },
    ])
    result = _all_media_from_group(post)
    assert len(result) == 1
    assert result[0].caption == "cap"
    # PTB stores caption_entities as a tuple of dicts (not MessageEntity objects).
    assert result[0].caption_entities is not None
    assert len(result[0].caption_entities) == 1
    ent = result[0].caption_entities[0]
    assert ent["type"] == "bold"
    assert ent["offset"] == 0
    assert ent["length"] == 3
