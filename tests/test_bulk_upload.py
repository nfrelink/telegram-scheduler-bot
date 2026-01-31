from __future__ import annotations

from dataclasses import dataclass
import json

import pytest
from telegram import MessageEntity

from database import queries as db
from handlers import bulk_upload


@dataclass
class _FakeUser:
    id: int
    username: str | None = "u"
    first_name: str | None = "f"
    last_name: str | None = "l"


class _FakeMessage:
    def __init__(self, *, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

        # Media-related attributes
        self.caption: str | None = None
        self.entities = None
        self.caption_entities = None
        self.photo = None
        self.video = None
        self.document = None
        self.media_group_id = None

    async def reply_text(self, text: str, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.replies.append(text)


@dataclass
class _FakeUpdate:
    message: _FakeMessage
    effective_user: _FakeUser | None = None
    effective_chat: object | None = None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}


class _FakePhoto:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


@pytest.mark.asyncio
async def test_bulk_confirm_inserts_posts_and_unpauses_empty_paused(initialized_db) -> None:
    user = _FakeUser(id=111)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-4004", channel_name="Channel 4")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="BulkSchedule",
        pattern={"type": "interval", "minutes": 10},
        timezone_name="UTC",
        state="empty_paused",
    )
    schedule_id = int(schedule["id"])

    context = _FakeContext()
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_posts"] = [
        {"media_type": "photo", "file_id": "p1", "file_path": None, "caption": "c1", "media_group_data": None},
        {"media_type": "video", "file_id": "v1", "file_path": None, "caption": None, "media_group_data": None},
    ]

    update = _FakeUpdate(message=_FakeMessage(text="yes"), effective_user=user)

    result = await bulk_upload.bulk_confirm(update, context)  # type: ignore[arg-type]
    assert result == bulk_upload.ConversationHandler.END  # type: ignore[attr-defined]

    posts = await db.get_queued_posts(schedule_id, limit=10)
    assert len(posts) == 2
    assert [p["file_id"] for p in posts] == ["p1", "v1"]
    assert [int(p["position"]) for p in posts] == [0, 1]

    schedule2 = await db.get_schedule(schedule_id)
    assert schedule2 is not None
    assert schedule2["state"] == "paused"

    # State cleared
    assert not any(k.startswith("bulk_") for k in context.user_data.keys())

    # User got a confirmation message
    assert any("Queued" in msg for msg in update.message.replies)


def test_message_to_collected_item_caption_modes() -> None:
    msg = _FakeMessage()
    msg.caption = "hello"
    msg.photo = [_FakePhoto("photo_file_id")]

    item_preserve = bulk_upload._message_to_collected_item(
        msg, caption_mode="preserve", single_caption=None, single_caption_entities=None
    )  # type: ignore[attr-defined]
    assert item_preserve is not None
    assert item_preserve.caption == "hello"

    item_remove = bulk_upload._message_to_collected_item(
        msg, caption_mode="remove", single_caption=None, single_caption_entities=None
    )  # type: ignore[attr-defined]
    assert item_remove is not None
    assert item_remove.caption is None

    item_single = bulk_upload._message_to_collected_item(
        msg, caption_mode="single", single_caption="SINGLE", single_caption_entities=None
    )  # type: ignore[attr-defined]
    assert item_single is not None
    assert item_single.caption == "SINGLE"


@pytest.mark.asyncio
async def test_media_group_collected_and_flushed_on_done(initialized_db) -> None:
    user = _FakeUser(id=222)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-5005", channel_name="Channel 5")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="MG",
        pattern={"type": "interval", "minutes": 10},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    context = _FakeContext()
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_caption_mode"] = "preserve"

    m1 = _FakeMessage()
    m1.caption = "cap"
    m1.photo = [_FakePhoto("p1")]
    m1.media_group_id = "gid"
    u1 = _FakeUpdate(message=m1, effective_user=user)
    st1 = await bulk_upload.bulk_collect_media(u1, context)  # type: ignore[arg-type]
    assert st1 == bulk_upload.COLLECTING_MEDIA

    m2 = _FakeMessage()
    m2.caption = None
    m2.photo = [_FakePhoto("p2")]
    m2.media_group_id = "gid"
    u2 = _FakeUpdate(message=m2, effective_user=user)
    st2 = await bulk_upload.bulk_collect_media(u2, context)  # type: ignore[arg-type]
    assert st2 == bulk_upload.COLLECTING_MEDIA

    done_msg = _FakeMessage(text="/done")
    done_update = _FakeUpdate(message=done_msg, effective_user=user)
    st_done = await bulk_upload.bulk_done(done_update, context)  # type: ignore[arg-type]
    assert st_done == bulk_upload.CONFIRMING

    posts = context.user_data.get("bulk_posts")
    assert isinstance(posts, list)
    assert len(posts) == 1
    assert posts[0]["media_type"] == "media_group"
    assert posts[0]["media_group_data"] is not None


@pytest.mark.asyncio
async def test_single_markdown_caption_sets_parse_mode_for_posts(initialized_db) -> None:
    user = _FakeUser(id=333)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-6006", channel_name="Channel 6")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="MD",
        pattern={"type": "interval", "minutes": 10},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    context = _FakeContext()
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_caption_mode"] = "single"

    # Simulate the user sending markdown-ish caption text (no Telegram entities).
    caption_msg = _FakeMessage(text="[t](https://example.com) | `x`")
    caption_update = _FakeUpdate(message=caption_msg, effective_user=user)
    st_caption = await bulk_upload.bulk_set_single_caption(caption_update, context)  # type: ignore[arg-type]
    assert st_caption == bulk_upload.COLLECTING_MEDIA

    media_msg = _FakeMessage()
    media_msg.photo = [_FakePhoto("p1")]
    media_update = _FakeUpdate(message=media_msg, effective_user=user)

    st = await bulk_upload.bulk_collect_media(media_update, context)  # type: ignore[arg-type]
    assert st == bulk_upload.COLLECTING_MEDIA

    posts = context.user_data.get("bulk_posts")
    assert isinstance(posts, list)
    assert len(posts) == 1
    assert posts[0]["caption_parse_mode"] is None
    assert posts[0]["caption"] == "t | x"
    assert posts[0]["caption_entities"] is not None
    ents = json.loads(posts[0]["caption_entities"])
    assert any(e.get("type") == "text_link" for e in ents)
    assert any(e.get("type") == "code" for e in ents)


@pytest.mark.asyncio
async def test_single_markdown_caption_sets_parse_mode_for_media_groups(initialized_db) -> None:
    user = _FakeUser(id=444)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-7007", channel_name="Channel 7")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="MDG",
        pattern={"type": "interval", "minutes": 10},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    context = _FakeContext()
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_caption_mode"] = "single"

    caption_msg = _FakeMessage(text="[t](https://example.com) | `x`")
    caption_update = _FakeUpdate(message=caption_msg, effective_user=user)
    st_caption = await bulk_upload.bulk_set_single_caption(caption_update, context)  # type: ignore[arg-type]
    assert st_caption == bulk_upload.COLLECTING_MEDIA

    m1 = _FakeMessage()
    m1.photo = [_FakePhoto("p1")]
    m1.media_group_id = "gid"
    u1 = _FakeUpdate(message=m1, effective_user=user)
    st1 = await bulk_upload.bulk_collect_media(u1, context)  # type: ignore[arg-type]
    assert st1 == bulk_upload.COLLECTING_MEDIA

    m2 = _FakeMessage()
    m2.photo = [_FakePhoto("p2")]
    m2.media_group_id = "gid"
    u2 = _FakeUpdate(message=m2, effective_user=user)
    st2 = await bulk_upload.bulk_collect_media(u2, context)  # type: ignore[arg-type]
    assert st2 == bulk_upload.COLLECTING_MEDIA

    done_msg = _FakeMessage(text="/done")
    done_update = _FakeUpdate(message=done_msg, effective_user=user)
    st_done = await bulk_upload.bulk_done(done_update, context)  # type: ignore[arg-type]
    assert st_done == bulk_upload.CONFIRMING

    posts = context.user_data.get("bulk_posts")
    assert isinstance(posts, list)
    assert len(posts) == 1
    assert posts[0]["media_type"] == "media_group"
    data = json.loads(posts[0]["media_group_data"])
    assert data[0]["caption_parse_mode"] is None
    assert data[0]["caption_entities"] is not None


@pytest.mark.asyncio
async def test_single_caption_keeps_telegram_entities_when_present(initialized_db) -> None:
    user = _FakeUser(id=555)
    await db.upsert_user(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, is_admin=False)

    channel = await db.create_channel(user_id=user.id, telegram_channel_id="-8008", channel_name="Channel 8")
    schedule = await db.create_schedule(
        channel_db_id=int(channel["id"]),
        name="Ent",
        pattern={"type": "interval", "minutes": 10},
        timezone_name="UTC",
        state="paused",
    )
    schedule_id = int(schedule["id"])

    context = _FakeContext()
    context.user_data["bulk_schedule_id"] = schedule_id
    context.user_data["bulk_caption_mode"] = "single"

    caption_msg = _FakeMessage(text="t | x")
    caption_msg.entities = [MessageEntity(type="code", offset=4, length=1)]
    caption_update = _FakeUpdate(message=caption_msg, effective_user=user)
    st_caption = await bulk_upload.bulk_set_single_caption(caption_update, context)  # type: ignore[arg-type]
    assert st_caption == bulk_upload.COLLECTING_MEDIA

    assert context.user_data["bulk_single_caption"] == "t | x"
    assert context.user_data.get("bulk_single_caption_entities") is not None

