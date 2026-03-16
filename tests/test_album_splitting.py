"""Tests for Phase 4 / Phase 8 album-splitting logic in src/handlers/bulk_upload.py."""
from __future__ import annotations

import json

from handlers.bulk_upload import (
    _CollectedItem,
    _apply_split_decisions,
    _build_split_prompt,
    _group_needs_split_prompt,
    _origin_link,
)


def _item(
    *,
    file_id: str = "f1",
    media_type: str = "photo",
    raw_origin_chat_id: int | None = None,
    raw_origin_message_id: int | None = None,
    raw_origin_is_forwarded: bool = False,
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
        raw_origin_message_id=raw_origin_message_id,
        raw_origin_is_forwarded=raw_origin_is_forwarded,
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
    # raw_origin_is_forwarded is False → locally uploaded, never prompt.
    items = [_item(raw_origin_is_forwarded=False)]
    assert _group_needs_split_prompt(items, allowlist=set()) is False


def test_group_needs_split_prompt_allowlisted_channel_returns_false() -> None:
    items = [_item(raw_origin_chat_id=-1001, raw_origin_is_forwarded=True)]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is False


def test_group_needs_split_prompt_non_allowlisted_channel_returns_true() -> None:
    items = [_item(raw_origin_chat_id=-1002, raw_origin_is_forwarded=True)]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is True


def test_group_needs_split_prompt_person_forwarded_returns_true() -> None:
    # Person-forwarded albums: raw_origin_chat_id is None (no channel ID),
    # but raw_origin_is_forwarded is True → should still prompt.
    items = [_item(raw_origin_chat_id=None, raw_origin_is_forwarded=True)]
    assert _group_needs_split_prompt(items, allowlist=set()) is True


def test_group_needs_split_prompt_person_forwarded_not_suppressed_by_allowlist() -> None:
    # Person-forwards are never in the allowlist (which only contains channel IDs).
    items = [_item(raw_origin_chat_id=None, raw_origin_is_forwarded=True)]
    assert _group_needs_split_prompt(items, allowlist={-1001, -1002}) is True


def test_group_needs_split_prompt_empty_allowlist_and_channel_forwarded_returns_true() -> None:
    items = [_item(raw_origin_chat_id=-9999, raw_origin_is_forwarded=True)]
    assert _group_needs_split_prompt(items, allowlist=set()) is True


def test_group_needs_split_prompt_only_first_item_is_checked() -> None:
    # Only items[0] is consulted per the implementation.
    items = [
        _item(raw_origin_chat_id=-1001, raw_origin_is_forwarded=True),  # allowlisted
        _item(raw_origin_chat_id=-9999, raw_origin_is_forwarded=True),  # not allowlisted — irrelevant
    ]
    assert _group_needs_split_prompt(items, allowlist={-1001}) is False


# ---------------------------------------------------------------------------
# _origin_link
# ---------------------------------------------------------------------------

def test_origin_link_returns_none_for_none_inputs() -> None:
    assert _origin_link(None, None) is None
    assert _origin_link(-1001234567890, None) is None
    assert _origin_link(None, 42) is None


def test_origin_link_returns_none_for_non_channel_id() -> None:
    # Positive user IDs or short negative IDs are not supergroup/channel format.
    assert _origin_link(12345, 42) is None
    assert _origin_link(-12345, 42) is None


def test_origin_link_builds_correct_url_for_channel() -> None:
    # -1001234567890 → inner_id = "1234567890"
    link = _origin_link(-1001234567890, 99)
    assert link == "https://t.me/c/1234567890/99"


def test_origin_link_strips_minus_100_prefix() -> None:
    link = _origin_link(-1009999999999, 1)
    assert link is not None
    assert link.startswith("https://t.me/c/")
    assert "-" not in link.split("t.me/c/")[1]


# ---------------------------------------------------------------------------
# _build_split_prompt
# ---------------------------------------------------------------------------

def _prompt(count: int = 2, idx: int = 0, **kwargs) -> tuple[str, object]:
    return _build_split_prompt(count, idx, album_num=1, total_albums=1, **kwargs)


def test_build_split_prompt_text_mentions_count() -> None:
    text, _ = _prompt(count=3)
    assert "3 item(s)" in text


def test_build_split_prompt_text_shows_album_progress() -> None:
    text, _ = _build_split_prompt(2, 0, album_num=2, total_albums=3)
    assert "Album 2 of 3" in text


def test_build_split_prompt_keyboard_has_exactly_two_buttons_without_link() -> None:
    _, kb = _prompt(count=2, idx=1)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert len(buttons) == 2


def test_build_split_prompt_adds_view_original_button_when_link_provided() -> None:
    _, kb = _prompt(count=2, idx=0, origin_link="https://t.me/c/123/456")
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    url_buttons = [btn for btn in all_buttons if getattr(btn, "url", None)]
    assert len(url_buttons) == 1
    assert url_buttons[0].url == "https://t.me/c/123/456"


def test_build_split_prompt_no_view_button_when_no_link() -> None:
    _, kb = _prompt(count=2, idx=0, origin_link=None)
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert not any(getattr(btn, "url", None) for btn in all_buttons)


def test_build_split_prompt_shows_caption_snippet() -> None:
    text, _ = _prompt(count=2, first_caption="A beautiful sunset")
    assert "A beautiful sunset" in text


def test_build_split_prompt_truncates_long_caption() -> None:
    long = "x" * 120
    text, _ = _prompt(count=2, first_caption=long)
    assert "..." in text
    assert len([line for line in text.splitlines() if "Caption" in line][0]) < 120


def test_build_split_prompt_no_caption_line_when_none() -> None:
    text, _ = _prompt(count=2, first_caption=None)
    assert "Caption" not in text


def test_build_split_prompt_callback_data_encodes_placeholder_idx() -> None:
    _, kb = _prompt(count=4, idx=2)
    datas = {btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data}
    assert "sp:keep:2" in datas
    assert "sp:split:2" in datas


def test_build_split_prompt_different_idx_produces_different_callbacks() -> None:
    _, kb0 = _prompt(count=2, idx=0)
    _, kb5 = _prompt(count=2, idx=5)
    datas0 = {btn.callback_data for row in kb0.inline_keyboard for btn in row if btn.callback_data}
    datas5 = {btn.callback_data for row in kb5.inline_keyboard for btn in row if btn.callback_data}
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
