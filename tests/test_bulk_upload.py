"""Tests for bulk_upload handler logic.

Covers pure/near-pure functions, state transitions, and error branches.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers.bulk_upload import (
    COLLECTING_MEDIA,
    SELECTING_CAPTION_MODE,
    WAITING_SINGLE_CAPTION,
    _CollectedItem,
    _apply_split_decisions,
    _finalize_media_group_items,
    _flush_media_group,
    _get_posts,
    _get_media_groups,
    _get_media_group_indexes,
    _load_staging_into_user_data,
    _parse_markdownish,
    bulk_collect_media,
    bulk_confirm,
    bulk_set_caption_mode,
    bulk_set_single_caption,
)
from telegram.ext import ConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    media_type: str = "photo",
    file_id: str = "fid_1",
    file_unique_id: str = "uniq_1",
    caption: str | None = None,
    caption_entities: list[dict[str, Any]] | None = None,
    forward_from_chat_id: int | None = None,
    forward_from_message_id: int | None = None,
    forward_origin_chat_id: int | None = None,
    forward_origin_message_id: int | None = None,
    raw_origin_chat_id: int | None = None,
    raw_origin_message_id: int | None = None,
    raw_origin_is_forwarded: bool = False,
) -> _CollectedItem:
    return _CollectedItem(
        media_type=media_type,
        file_id=file_id,
        file_unique_id=file_unique_id,
        caption=caption,
        caption_entities=caption_entities,
        forward_from_chat_id=forward_from_chat_id,
        forward_from_message_id=forward_from_message_id,
        forward_origin_chat_id=forward_origin_chat_id,
        forward_origin_message_id=forward_origin_message_id,
        raw_origin_chat_id=raw_origin_chat_id,
        raw_origin_message_id=raw_origin_message_id,
        raw_origin_is_forwarded=raw_origin_is_forwarded,
    )


def _mock_context(**user_data_init: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = dict(user_data_init)
    ctx.bot = AsyncMock()
    ctx.args = []
    return ctx


def _mock_update(
    *,
    user_id: int = 42,
    text: str | None = None,
    photo: bool = False,
    video: bool = False,
    document: bool = False,
    media_group_id: str | None = None,
    chat_type: str = "private",
    caption: str | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type

    message = MagicMock()
    message.text = text
    message.caption = caption
    message.media_group_id = media_group_id
    message.reply_text = AsyncMock()

    # Media fields
    if photo:
        photo_obj = MagicMock()
        photo_obj.file_id = "photo_fid"
        photo_obj.file_unique_id = "photo_uniq"
        message.photo = [photo_obj]
    else:
        message.photo = None

    if video:
        video_obj = MagicMock()
        video_obj.file_id = "video_fid"
        video_obj.file_unique_id = "video_uniq"
        message.video = video_obj
    else:
        message.video = None

    if document:
        doc_obj = MagicMock()
        doc_obj.file_id = "doc_fid"
        doc_obj.file_unique_id = "doc_uniq"
        message.document = doc_obj
    else:
        message.document = None

    message.caption_entities = None
    message.entities = None
    message.forward_from_chat = None
    message.forward_from = None
    message.forward_origin = None
    message.forward_from_message_id = None

    update.message = message
    update.callback_query = None
    return update


# ===========================================================================
# _finalize_media_group_items
# ===========================================================================


class TestFinalizeMediaGroupItems:
    def test_basic_preserve_caption(self) -> None:
        items = [
            _make_item(file_id="a", caption="cap1"),
            _make_item(file_id="b", caption="cap2"),
        ]
        result = json.loads(
            _finalize_media_group_items(
                items,
                caption_mode="preserve",
                single_caption=None,
                single_caption_entities=None,
            )
        )
        assert len(result) == 2
        assert result[0]["file_id"] == "a"
        assert result[1]["file_id"] == "b"
        # preserve mode: group caption is None (individual captions are on items
        # but the function uses single/remove logic for the group)
        assert result[0]["caption"] is None
        assert result[1]["caption"] is None

    def test_single_caption_applied_to_first_item_only(self) -> None:
        items = [_make_item(file_id="a"), _make_item(file_id="b")]
        result = json.loads(
            _finalize_media_group_items(
                items,
                caption_mode="single",
                single_caption="shared caption",
                single_caption_entities=[{"type": "bold", "offset": 0, "length": 6}],
            )
        )
        assert result[0]["caption"] == "shared caption"
        assert result[0]["caption_entities"] == [
            {"type": "bold", "offset": 0, "length": 6}
        ]
        assert result[1]["caption"] is None
        assert result[1]["caption_entities"] is None

    def test_remove_caption(self) -> None:
        items = [_make_item(file_id="a", caption="will be removed")]
        result = json.loads(
            _finalize_media_group_items(
                items,
                caption_mode="remove",
                single_caption=None,
                single_caption_entities=None,
            )
        )
        assert result[0]["caption"] is None

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty media group"):
            _finalize_media_group_items(
                [],
                caption_mode="remove",
                single_caption=None,
                single_caption_entities=None,
            )

    def test_items_sorted_by_forward_message_id(self) -> None:
        items = [
            _make_item(file_id="b", forward_from_message_id=20),
            _make_item(file_id="a", forward_from_message_id=10),
        ]
        result = json.loads(
            _finalize_media_group_items(
                items,
                caption_mode="remove",
                single_caption=None,
                single_caption_entities=None,
            )
        )
        assert result[0]["file_id"] == "a"
        assert result[1]["file_id"] == "b"

    def test_forward_fields_preserved(self) -> None:
        items = [
            _make_item(
                file_id="a",
                forward_from_chat_id=111,
                forward_from_message_id=222,
                forward_origin_chat_id=333,
                forward_origin_message_id=444,
            )
        ]
        result = json.loads(
            _finalize_media_group_items(
                items,
                caption_mode="remove",
                single_caption=None,
                single_caption_entities=None,
            )
        )
        assert result[0]["forward_from_chat_id"] == 111
        assert result[0]["forward_from_message_id"] == 222
        assert result[0]["forward_origin_chat_id"] == 333
        assert result[0]["forward_origin_message_id"] == 444


# ===========================================================================
# _flush_media_group
# ===========================================================================


class TestFlushMediaGroup:
    @pytest.mark.asyncio
    async def test_updates_placeholder_at_correct_index(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        posts = _get_posts(ctx)
        posts.append({"media_type": "photo", "file_id": "solo"})
        posts.append(
            {
                "media_type": "media_group",
                "file_id": None,
                "file_path": None,
                "caption": None,
                "caption_parse_mode": None,
                "caption_entities": None,
                "media_group_data": None,
            }
        )

        groups = _get_media_groups(ctx)
        groups["grp1"] = [_make_item(file_id="g1"), _make_item(file_id="g2")]
        indexes = _get_media_group_indexes(ctx)
        indexes["grp1"] = 1

        await _flush_media_group(ctx, group_id="grp1")

        assert posts[0]["file_id"] == "solo"
        assert posts[1]["media_type"] == "media_group"
        assert posts[1]["media_group_data"] is not None
        data = json.loads(posts[1]["media_group_data"])
        assert len(data) == 2
        assert "grp1" not in groups
        assert "grp1" not in indexes

    @pytest.mark.asyncio
    async def test_appends_when_no_placeholder(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        groups = _get_media_groups(ctx)
        groups["grp2"] = [_make_item(file_id="x")]

        await _flush_media_group(ctx, group_id="grp2")

        posts = _get_posts(ctx)
        assert len(posts) == 1
        assert posts[0]["media_type"] == "media_group"
        assert posts[0]["media_group_data"] is not None

    @pytest.mark.asyncio
    async def test_noop_when_no_items(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        await _flush_media_group(ctx, group_id="nonexistent")
        assert _get_posts(ctx) == []

    @pytest.mark.asyncio
    async def test_noop_when_no_caption_mode(self) -> None:
        ctx = _mock_context()
        groups = _get_media_groups(ctx)
        groups["grp3"] = [_make_item()]

        await _flush_media_group(ctx, group_id="grp3")
        assert _get_posts(ctx) == []


# ===========================================================================
# _apply_split_decisions
# ===========================================================================


class TestApplySplitDecisions:
    def test_keep_produces_media_group_post(self) -> None:
        ctx = _mock_context()
        posts = _get_posts(ctx)
        posts.append({"media_type": "photo", "file_id": "solo"})
        posts.append(
            {
                "media_type": "media_group",
                "file_id": None,
                "media_group_data": None,
            }
        )
        items = [_make_item(file_id="g1"), _make_item(file_id="g2")]
        ctx.user_data["bulk_split_decisions"] = {1: ("keep", items)}

        _apply_split_decisions(
            ctx,
            caption_mode="remove",
            single_caption=None,
            single_caption_entities=None,
        )

        result = ctx.user_data["bulk_posts"]
        assert len(result) == 2
        assert result[0]["file_id"] == "solo"
        assert result[1]["media_type"] == "media_group"
        assert result[1]["media_group_data"] is not None

    def test_split_produces_individual_posts(self) -> None:
        ctx = _mock_context()
        posts = _get_posts(ctx)
        posts.append(
            {
                "media_type": "media_group",
                "file_id": None,
                "media_group_data": None,
            }
        )
        items = [
            _make_item(media_type="photo", file_id="g1", caption="c1"),
            _make_item(media_type="video", file_id="g2", caption="c2"),
        ]
        ctx.user_data["bulk_split_decisions"] = {0: ("split", items)}

        _apply_split_decisions(
            ctx,
            caption_mode="preserve",
            single_caption=None,
            single_caption_entities=None,
        )

        result = ctx.user_data["bulk_posts"]
        assert len(result) == 2
        assert result[0]["media_type"] == "photo"
        assert result[0]["file_id"] == "g1"
        assert result[1]["media_type"] == "video"
        assert result[1]["file_id"] == "g2"

    def test_no_decisions_is_noop(self) -> None:
        ctx = _mock_context()
        posts = _get_posts(ctx)
        posts.append({"media_type": "photo", "file_id": "solo"})

        _apply_split_decisions(
            ctx,
            caption_mode="remove",
            single_caption=None,
            single_caption_entities=None,
        )
        assert ctx.user_data["bulk_posts"] == [
            {"media_type": "photo", "file_id": "solo"}
        ]

    def test_mixed_decisions_and_passthrough(self) -> None:
        ctx = _mock_context()
        posts = _get_posts(ctx)
        posts.append({"media_type": "photo", "file_id": "p1"})
        posts.append(
            {"media_type": "media_group", "file_id": None, "media_group_data": None}
        )
        posts.append({"media_type": "video", "file_id": "v1"})

        items = [_make_item(file_id="g1"), _make_item(file_id="g2")]
        ctx.user_data["bulk_split_decisions"] = {1: ("split", items)}

        _apply_split_decisions(
            ctx,
            caption_mode="remove",
            single_caption=None,
            single_caption_entities=None,
        )

        result = ctx.user_data["bulk_posts"]
        assert len(result) == 4
        assert result[0]["file_id"] == "p1"
        assert result[1]["file_id"] == "g1"
        assert result[2]["file_id"] == "g2"
        assert result[3]["file_id"] == "v1"


# ===========================================================================
# _parse_markdownish
# ===========================================================================


class TestParseMarkdownish:
    def test_inline_link(self) -> None:
        text, entities = _parse_markdownish("[click](https://example.com)")
        assert text == "click"
        assert entities is not None
        assert len(entities) == 1
        assert entities[0]["type"] == "text_link"
        assert entities[0]["url"] == "https://example.com"

    def test_inline_code(self) -> None:
        text, entities = _parse_markdownish("run `ls -la` now")
        assert text == "run ls -la now"
        assert entities is not None
        assert len(entities) == 1
        assert entities[0]["type"] == "code"

    def test_plain_text(self) -> None:
        text, entities = _parse_markdownish("no markup here")
        assert text == "no markup here"
        assert entities is None

    def test_escaped_characters(self) -> None:
        text, entities = _parse_markdownish("\\[not a link\\]")
        assert text == "[not a link]"
        assert entities is None


# ===========================================================================
# bulk_collect_media — state transitions + error branches
# ===========================================================================


class TestBulkCollectMedia:
    @pytest.mark.asyncio
    async def test_photo_returns_collecting_media(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        update = _mock_update(photo=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                result = await bulk_collect_media(update, ctx)

        assert result == COLLECTING_MEDIA
        posts = ctx.user_data["bulk_posts"]
        assert len(posts) == 1
        assert posts[0]["media_type"] == "photo"
        assert posts[0]["file_id"] == "photo_fid"

    @pytest.mark.asyncio
    async def test_video_collected(self) -> None:
        ctx = _mock_context(bulk_caption_mode="preserve")
        update = _mock_update(video=True, caption="vid caption")

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                result = await bulk_collect_media(update, ctx)

        assert result == COLLECTING_MEDIA
        posts = ctx.user_data["bulk_posts"]
        assert len(posts) == 1
        assert posts[0]["media_type"] == "video"

    @pytest.mark.asyncio
    async def test_document_collected(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        update = _mock_update(document=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                result = await bulk_collect_media(update, ctx)

        assert result == COLLECTING_MEDIA
        assert ctx.user_data["bulk_posts"][0]["media_type"] == "document"

    @pytest.mark.asyncio
    async def test_unsupported_message_type(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        update = _mock_update()  # no media at all

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                result = await bulk_collect_media(update, ctx)

        assert result == COLLECTING_MEDIA
        update.message.reply_text.assert_called_once()
        assert "Unsupported" in update.message.reply_text.call_args[0][0]
        assert _get_posts(ctx) == []

    @pytest.mark.asyncio
    async def test_missing_caption_mode_ends_conversation(self) -> None:
        ctx = _mock_context()  # no bulk_caption_mode
        update = _mock_update(photo=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            result = await bulk_collect_media(update, ctx)

        assert result == ConversationHandler.END
        assert "Caption mode missing" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_missing_single_caption_ends_conversation(self) -> None:
        ctx = _mock_context(bulk_caption_mode="single")
        update = _mock_update(photo=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            result = await bulk_collect_media(update, ctx)

        assert result == ConversationHandler.END
        assert "Single caption missing" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_media_group_buffering(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        update1 = _mock_update(photo=True, media_group_id="mg1")
        update2 = _mock_update(video=True, media_group_id="mg1")

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                r1 = await bulk_collect_media(update1, ctx)
                r2 = await bulk_collect_media(update2, ctx)

        assert r1 == COLLECTING_MEDIA
        assert r2 == COLLECTING_MEDIA
        # One placeholder in posts
        posts = _get_posts(ctx)
        assert len(posts) == 1
        assert posts[0]["media_type"] == "media_group"
        # Two items buffered in groups
        groups = _get_media_groups(ctx)
        assert len(groups["mg1"]) == 2
        # Index recorded once
        indexes = _get_media_group_indexes(ctx)
        assert indexes["mg1"] == 0

    @pytest.mark.asyncio
    async def test_media_during_confirming_returns_to_collecting(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove", bulk_in_confirming=True)
        update = _mock_update(photo=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                result = await bulk_collect_media(update, ctx)

        assert result == COLLECTING_MEDIA
        assert ctx.user_data.get("bulk_in_confirming") is False

    @pytest.mark.asyncio
    async def test_staging_item_persisted(self) -> None:
        ctx = _mock_context(bulk_caption_mode="remove")
        update = _mock_update(photo=True)

        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.get_forward_origin_allowlist = AsyncMock(return_value=[])
                mock_db.add_staging_item = AsyncMock()
                await bulk_collect_media(update, ctx)

        mock_db.add_staging_item.assert_called_once()
        call_kwargs = mock_db.add_staging_item.call_args
        assert call_kwargs[0][0] == 42  # user_id
        assert call_kwargs[1]["media_type"] == "photo"
        assert call_kwargs[1]["file_id"] == "photo_fid"


# ===========================================================================
# bulk_set_caption_mode — state transitions
# ===========================================================================


class TestBulkSetCaptionMode:
    @pytest.mark.asyncio
    async def test_remove_goes_to_collecting(self) -> None:
        ctx = _mock_context(bulk_schedule_id=1)
        update = _mock_update(text="remove")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.create_bulk_session = AsyncMock()
                result = await bulk_set_caption_mode(update, ctx)
        assert result == COLLECTING_MEDIA
        assert ctx.user_data["bulk_caption_mode"] == "remove"

    @pytest.mark.asyncio
    async def test_single_goes_to_waiting_caption(self) -> None:
        ctx = _mock_context(bulk_schedule_id=1)
        update = _mock_update(text="single")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.create_bulk_session = AsyncMock()
                result = await bulk_set_caption_mode(update, ctx)
        assert result == WAITING_SINGLE_CAPTION
        assert ctx.user_data["bulk_caption_mode"] == "single"

    @pytest.mark.asyncio
    async def test_preserve_goes_to_collecting(self) -> None:
        ctx = _mock_context(bulk_schedule_id=1)
        update = _mock_update(text="preserve")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.create_bulk_session = AsyncMock()
                result = await bulk_set_caption_mode(update, ctx)
        assert result == COLLECTING_MEDIA

    @pytest.mark.asyncio
    async def test_invalid_stays_in_selecting(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="invalid")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            result = await bulk_set_caption_mode(update, ctx)
        assert result == SELECTING_CAPTION_MODE

    @pytest.mark.asyncio
    async def test_markdown_alias_maps_to_single(self) -> None:
        ctx = _mock_context(bulk_schedule_id=1)
        update = _mock_update(text="markdown")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.create_bulk_session = AsyncMock()
                result = await bulk_set_caption_mode(update, ctx)
        assert result == WAITING_SINGLE_CAPTION
        assert ctx.user_data["bulk_caption_mode"] == "single"

    @pytest.mark.asyncio
    async def test_session_persisted_to_db(self) -> None:
        ctx = _mock_context(bulk_schedule_id=7)
        update = _mock_update(text="remove")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.create_bulk_session = AsyncMock()
                await bulk_set_caption_mode(update, ctx)
        mock_db.create_bulk_session.assert_called_once_with(
            42, schedule_id=7, caption_mode="remove"
        )


# ===========================================================================
# bulk_set_single_caption — state transitions
# ===========================================================================


class TestBulkSetSingleCaption:
    @pytest.mark.asyncio
    async def test_valid_caption_goes_to_collecting(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="My caption")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.bulk_upload.db") as mock_db:
                mock_db.update_bulk_session_caption = AsyncMock()
                result = await bulk_set_single_caption(update, ctx)
        assert result == COLLECTING_MEDIA
        assert ctx.user_data["bulk_single_caption"] == "My caption"

    @pytest.mark.asyncio
    async def test_empty_caption_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="   ")
        with patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock):
            result = await bulk_set_single_caption(update, ctx)
        assert result == WAITING_SINGLE_CAPTION


# ===========================================================================
# _load_staging_into_user_data — resume reconstruction
# ===========================================================================


class TestLoadStagingIntoUserData:
    def test_individual_items_loaded(self) -> None:
        ctx = _mock_context()
        session = {
            "schedule_id": 5,
            "caption_mode": "remove",
            "single_caption": None,
            "single_caption_entities": None,
        }
        items = [
            {
                "media_type": "photo",
                "file_id": "f1",
                "caption": None,
                "caption_entities": None,
                "media_group_id": None,
                "forward_from_chat_id": None,
                "forward_from_message_id": None,
                "forward_origin_chat_id": None,
                "forward_origin_message_id": None,
                "raw_origin_chat_id": None,
                "raw_origin_message_id": None,
                "raw_origin_is_forwarded": False,
            },
            {
                "media_type": "video",
                "file_id": "f2",
                "caption": "cap",
                "caption_entities": None,
                "media_group_id": None,
                "forward_from_chat_id": None,
                "forward_from_message_id": None,
                "forward_origin_chat_id": None,
                "forward_origin_message_id": None,
                "raw_origin_chat_id": None,
                "raw_origin_message_id": None,
                "raw_origin_is_forwarded": False,
            },
        ]
        _load_staging_into_user_data(ctx, items, session)

        assert ctx.user_data["bulk_schedule_id"] == 5
        assert ctx.user_data["bulk_caption_mode"] == "remove"
        posts = _get_posts(ctx)
        assert len(posts) == 2
        assert posts[0]["media_type"] == "photo"
        assert posts[1]["media_type"] == "video"
        assert posts[1]["caption"] == "cap"

    def test_media_group_items_reconstructed(self) -> None:
        ctx = _mock_context()
        session = {
            "schedule_id": 5,
            "caption_mode": "remove",
            "single_caption": None,
            "single_caption_entities": None,
        }
        items = [
            {
                "media_type": "photo",
                "file_id": "solo",
                "caption": None,
                "caption_entities": None,
                "media_group_id": None,
                "forward_from_chat_id": None,
                "forward_from_message_id": None,
                "forward_origin_chat_id": None,
                "forward_origin_message_id": None,
                "raw_origin_chat_id": None,
                "raw_origin_message_id": None,
                "raw_origin_is_forwarded": False,
            },
            {
                "media_type": "photo",
                "file_id": "g1",
                "caption": None,
                "caption_entities": None,
                "media_group_id": "mg1",
                "forward_from_chat_id": None,
                "forward_from_message_id": None,
                "forward_origin_chat_id": None,
                "forward_origin_message_id": None,
                "raw_origin_chat_id": None,
                "raw_origin_message_id": None,
                "raw_origin_is_forwarded": False,
            },
            {
                "media_type": "video",
                "file_id": "g2",
                "caption": None,
                "caption_entities": None,
                "media_group_id": "mg1",
                "forward_from_chat_id": None,
                "forward_from_message_id": None,
                "forward_origin_chat_id": None,
                "forward_origin_message_id": None,
                "raw_origin_chat_id": None,
                "raw_origin_message_id": None,
                "raw_origin_is_forwarded": False,
            },
        ]
        _load_staging_into_user_data(ctx, items, session)

        posts = _get_posts(ctx)
        assert len(posts) == 2  # 1 solo + 1 group placeholder
        assert posts[0]["media_type"] == "photo"
        assert posts[0]["file_id"] == "solo"
        assert posts[1]["media_type"] == "media_group"

        groups = _get_media_groups(ctx)
        assert "mg1" in groups
        assert len(groups["mg1"]) == 2
        assert groups["mg1"][0].file_id == "g1"
        assert groups["mg1"][1].file_id == "g2"

        indexes = _get_media_group_indexes(ctx)
        assert indexes["mg1"] == 1

    def test_single_caption_session_loaded(self) -> None:
        ctx = _mock_context()
        session = {
            "schedule_id": 3,
            "caption_mode": "single",
            "single_caption": "shared",
            "single_caption_entities": '[{"type": "bold", "offset": 0, "length": 6}]',
        }
        _load_staging_into_user_data(ctx, [], session)

        assert ctx.user_data["bulk_caption_mode"] == "single"
        assert ctx.user_data["bulk_single_caption"] == "shared"
        assert ctx.user_data["bulk_single_caption_entities"] == [
            {"type": "bold", "offset": 0, "length": 6}
        ]


# ===========================================================================
# bulk_confirm: post-enqueue state-transition policy
# ===========================================================================


class TestBulkConfirmAutoResume:
    """Distinguishes system-paused (empty_paused) from user-paused.

    Adding posts removes the only reason a schedule was empty_paused, so the
    handler should auto-resume. User-paused schedules are an explicit choice
    and must stay paused; active schedules are unchanged.
    """

    @staticmethod
    def _ctx_with_posts() -> MagicMock:
        ctx = _mock_context(
            bulk_schedule_id=7,
            bulk_channel_db_id=3,
            bulk_posts=[{"media_type": "photo", "file_id": "fid_1"}],
            bulk_fingerprint_data=[],
        )
        return ctx

    @staticmethod
    async def _run(
        ctx: MagicMock, schedule_state: str
    ) -> tuple[AsyncMock, AsyncMock, int]:
        update = _mock_update(text="yes")
        with (
            patch("handlers.bulk_upload.ensure_user_record", new_callable=AsyncMock),
            patch("handlers.bulk_upload._clear_staging", new_callable=AsyncMock),
            patch("handlers.bulk_upload.posting") as mock_posting,
            patch("handlers.bulk_upload.scheduling") as mock_scheduling,
            patch("handlers.bulk_upload.db") as mock_db,
        ):
            mock_posting.enqueue_bulk = AsyncMock(return_value=(1, [101]))
            mock_scheduling.resume = AsyncMock()
            mock_scheduling.pause = AsyncMock()
            mock_db.get_schedule_for_user = AsyncMock(
                return_value={"id": 7, "name": "test", "state": schedule_state}
            )
            mock_db.get_user_context_details = AsyncMock(return_value={})
            result = await bulk_confirm(update, ctx)
        return mock_scheduling.resume, mock_scheduling.pause, result

    @pytest.mark.asyncio
    async def test_empty_paused_is_auto_resumed(self) -> None:
        resume, pause, result = await self._run(self._ctx_with_posts(), "empty_paused")
        resume.assert_awaited_once_with(7, user_id=42)
        pause.assert_not_awaited()
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_user_paused_is_left_untouched(self) -> None:
        resume, pause, _ = await self._run(self._ctx_with_posts(), "paused")
        resume.assert_not_awaited()
        pause.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_is_left_untouched(self) -> None:
        resume, pause, _ = await self._run(self._ctx_with_posts(), "active")
        resume.assert_not_awaited()
        pause.assert_not_awaited()
