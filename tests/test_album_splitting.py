"""Tests for Phase 4 album-splitting logic in src/handlers/bulk_upload.py."""
from __future__ import annotations

import json

from handlers.bulk_upload import (
    _CollectedItem,
    _apply_split_decisions,
    _build_split_prompt,
    _group_needs_split_prompt,
)


def _item(
    *,
    file_id: str = "f1",
    media_type: str = "photo",
    raw_origin_chat_id: int | None = None,
    caption: str | None = None,
    forward_from_message_id: int | None = None,
) -> _CollectedItem:
    return _CollectedItem(
        media_type=media_type,
        file_id=file_id,
        caption=caption,
        caption_entities=None,
        forward_from_chat_id=None,
        forward_from_message_id=forward_from_message_id,
        forward_origin_chat_id=None,
        forward_origin_message_id=None,
        raw_origin_chat_id=raw_origin_chat_id,
    )


class _FakeContext:
    def __init__(self, user_data: dict) -> None:
        self.user_data = user_data


# ---------------------------------------------------------------------------
# _group_needs_split_prompt
# ---------------------------------------------------------------------------

def test_group_needs_split_prompt_empty_items_returns_false() -> None:
    assert _group_needs_split_prompt([], allowlist=set()) is False


def test_group_needs_split_prompt_local_upload_returns_false() -> None:
    # raw_origin_chat_id is None → locally uploaded, never prompt.
    items = [_item(raw_origin_chat_id=None)]
    assert _group_needs_split_prompt(items, allowlist=set()) is False


def test_group_needs_split_prompt_allowlisted_origin_returns_false() -> None:
    items = [_item(raw_origin_chat_id=-1001)]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is False


def test_group_needs_split_prompt_non_allowlisted_forward_returns_true() -> None:
    items = [_item(raw_origin_chat_id=-1002)]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is True


def test_group_needs_split_prompt_empty_allowlist_and_forwarded_returns_true() -> None:
    items = [_item(raw_origin_chat_id=-9999)]
    assert _group_needs_split_prompt(items, allowlist=set()) is True


def test_group_needs_split_prompt_only_first_item_origin_is_checked() -> None:
    # Only items[0].raw_origin_chat_id is consulted per the implementation.
    items = [
        _item(raw_origin_chat_id=-1001),  # allowlisted
        _item(raw_origin_chat_id=-9999),  # not allowlisted — irrelevant
    ]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is False


# ---------------------------------------------------------------------------
# _build_split_prompt
# ---------------------------------------------------------------------------

def test_build_split_prompt_text_mentions_count() -> None:
    text, _ = _build_split_prompt(count=3, placeholder_idx=0)
    assert "3 item(s)" in text


def test_build_split_prompt_keyboard_has_exactly_two_buttons() -> None:
    _, kb = _build_split_prompt(count=2, placeholder_idx=1)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert len(buttons) == 2


def test_build_split_prompt_callback_data_encodes_placeholder_idx() -> None:
    _, kb = _build_split_prompt(count=4, placeholder_idx=2)
    datas = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "sp:keep:2" in datas
    assert "sp:split:2" in datas


def test_build_split_prompt_different_idx_produces_different_callbacks() -> None:
    _, kb0 = _build_split_prompt(count=2, placeholder_idx=0)
    _, kb5 = _build_split_prompt(count=2, placeholder_idx=5)
    datas0 = {btn.callback_data for row in kb0.inline_keyboard for btn in row}
    datas5 = {btn.callback_data for row in kb5.inline_keyboard for btn in row}
    assert "sp:keep:0" in datas0
    assert "sp:keep:5" in datas5


# ---------------------------------------------------------------------------
# _apply_split_decisions
# ---------------------------------------------------------------------------

def _placeholder() -> dict:
    return {"media_type": "media_group", "file_id": None, "caption": None, "media_group_data": None}


def test_apply_split_decisions_no_decisions_leaves_posts_unchanged() -> None:
    post = _placeholder()
    ctx = _FakeContext({"bulk_posts": [post], "bulk_split_decisions": {}})
    _apply_split_decisions(ctx, caption_mode="remove", single_caption=None, single_caption_entities=None)  # type: ignore[arg-type]
    assert ctx.user_data["bulk_posts"] == [post]


def test_apply_split_decisions_keep_produces_single_media_group_post() -> None:
    items = [
        _item(file_id="fa", media_type="photo"),
        _item(file_id="fb", media_type="photo"),
    ]
    ctx = _FakeContext({
        "bulk_posts": [_placeholder()],
        "bulk_split_decisions": {0: ("keep", items)},
    })
    _apply_split_decisions(ctx, caption_mode="remove", single_caption=None, single_caption_entities=None)  # type: ignore[arg-type]

    posts = ctx.user_data["bulk_posts"]
    assert len(posts) == 1
    assert posts[0]["media_type"] == "media_group"
    assert posts[0]["media_group_data"] is not None

    items_data = json.loads(posts[0]["media_group_data"])
    assert len(items_data) == 2
    assert {i["file_id"] for i in items_data} == {"fa", "fb"}


def test_apply_split_decisions_keep_with_single_caption_applies_to_first_item() -> None:
    items = [
        _item(file_id="fa", media_type="photo"),
        _item(file_id="fb", media_type="photo"),
    ]
    ctx = _FakeContext({
        "bulk_posts": [_placeholder()],
        "bulk_split_decisions": {0: ("keep", items)},
    })
    _apply_split_decisions(
        ctx,  # type: ignore[arg-type]
        caption_mode="single",
        single_caption="My caption",
        single_caption_entities=None,
    )

    items_data = json.loads(ctx.user_data["bulk_posts"][0]["media_group_data"])
    assert items_data[0]["caption"] == "My caption"
    assert items_data[1]["caption"] is None


def test_apply_split_decisions_split_produces_individual_posts() -> None:
    items = [
        _item(file_id="fa", media_type="photo"),
        _item(file_id="fb", media_type="video"),
        _item(file_id="fc", media_type="photo"),
    ]
    ctx = _FakeContext({
        "bulk_posts": [_placeholder()],
        "bulk_split_decisions": {0: ("split", items)},
    })
    _apply_split_decisions(ctx, caption_mode="remove", single_caption=None, single_caption_entities=None)  # type: ignore[arg-type]

    posts = ctx.user_data["bulk_posts"]
    assert len(posts) == 3
    assert posts[0]["media_type"] == "photo"
    assert posts[0]["file_id"] == "fa"
    assert posts[1]["media_type"] == "video"
    assert posts[1]["file_id"] == "fb"
    assert posts[2]["media_type"] == "photo"
    assert posts[2]["file_id"] == "fc"
    for p in posts:
        assert p["media_group_data"] is None


def test_apply_split_decisions_passthrough_post_preserved() -> None:
    items = [_item(file_id="fx", media_type="photo")]
    passthrough = {"media_type": "photo", "file_id": "gy", "caption": None, "media_group_data": None}
    ctx = _FakeContext({
        "bulk_posts": [_placeholder(), passthrough],
        "bulk_split_decisions": {0: ("keep", items)},
    })
    _apply_split_decisions(ctx, caption_mode="remove", single_caption=None, single_caption_entities=None)  # type: ignore[arg-type]

    posts = ctx.user_data["bulk_posts"]
    assert len(posts) == 2
    assert posts[0]["media_type"] == "media_group"
    assert posts[1] == passthrough
