"""Post execution logic (send queued posts to channels)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo, MessageEntity
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ExtBot

logger = logging.getLogger(__name__)

_FILE_ID_ERROR_RE = re.compile(r"(file[_ ]?id|file identifier|wrong file)", re.IGNORECASE)


async def send_post(bot: ExtBot, *, telegram_channel_id: str, post: dict[str, Any]) -> bool:
    """Send a queued post to a Telegram channel.

    Supported media_type:
    - photo
    - video
    - document
    - media_group
    """
    media_type = post.get("media_type")
    caption = post.get("caption")
    caption_parse_mode = post.get("caption_parse_mode")
    caption_entities = post.get("caption_entities")

    file_id = post.get("file_id")
    file_path = post.get("file_path")

    try:
        return await _send_post_once(
            bot,
            telegram_channel_id=telegram_channel_id,
            post=post,
            media_type=media_type,
            caption=caption,
            caption_parse_mode=caption_parse_mode,
            caption_entities=caption_entities,
            file_id=file_id,
            file_path=file_path,
        )
    except Exception as e:
        # Fallback: if file_id posting fails, download and re-upload once.
        if (
            file_id
            and not file_path
            and media_type in {"photo", "video", "document"}
            and _looks_like_file_id_error(e)
        ):
            ok = await _retry_with_download(
                bot,
                telegram_channel_id=telegram_channel_id,
                media_type=media_type,
                file_id=file_id,
                caption=caption,
                caption_parse_mode=caption_parse_mode,
                caption_entities=caption_entities,
            )
            if ok:
                return True

        logger.error(
            "Failed to send post id=%s to channel=%s: %s",
            post.get("id"),
            telegram_channel_id,
            e,
            exc_info=True,
        )
        return False


async def _send_post_once(
    bot: ExtBot,
    *,
    telegram_channel_id: str,
    post: dict[str, Any],
    media_type: str | None,
    caption: str | None,
    caption_parse_mode: str | None,
    caption_entities: Any,
    file_id: str | None,
    file_path: str | None,
) -> bool:
    entities = _decode_entities(caption_entities)
    parse_mode = None if entities else _to_parse_mode(caption_parse_mode)

    match media_type:
        case "photo":
            payload = _resolve_file_ref(file_id=file_id, file_path=file_path)
            if isinstance(payload, Path):
                with payload.open("rb") as f:
                    await bot.send_photo(
                        chat_id=telegram_channel_id,
                        photo=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        caption_entities=entities,
                    )
            else:
                await bot.send_photo(
                    chat_id=telegram_channel_id,
                    photo=payload,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            return True

        case "video":
            payload = _resolve_file_ref(file_id=file_id, file_path=file_path)
            if isinstance(payload, Path):
                with payload.open("rb") as f:
                    await bot.send_video(
                        chat_id=telegram_channel_id,
                        video=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        caption_entities=entities,
                    )
            else:
                await bot.send_video(
                    chat_id=telegram_channel_id,
                    video=payload,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            return True

        case "document":
            payload = _resolve_file_ref(file_id=file_id, file_path=file_path)
            if isinstance(payload, Path):
                with payload.open("rb") as f:
                    await bot.send_document(
                        chat_id=telegram_channel_id,
                        document=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        caption_entities=entities,
                    )
            else:
                await bot.send_document(
                    chat_id=telegram_channel_id,
                    document=payload,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            return True

        case "media_group":
            media_group_data = post.get("media_group_data")
            if not media_group_data:
                raise ValueError("media_group_data missing")

            with ExitStack() as stack:
                media = _parse_media_group(media_group_data, stack=stack)
                await bot.send_media_group(chat_id=telegram_channel_id, media=media)
            return True

        case _:
            raise ValueError(f"Unsupported media_type: {media_type}")


def _looks_like_file_id_error(exc: Exception) -> bool:
    if isinstance(exc, BadRequest):
        return bool(_FILE_ID_ERROR_RE.search(str(exc)))
    return bool(_FILE_ID_ERROR_RE.search(str(exc)))


async def _retry_with_download(
    bot: ExtBot,
    *,
    telegram_channel_id: str,
    media_type: str,
    file_id: str,
    caption: str | None,
    caption_parse_mode: str | None,
    caption_entities: Any,
) -> bool:
    """Download by file_id and retry sending once.

    This is a best-effort fallback when a stored file_id becomes invalid.
    """
    temp_dir = Path(os.getenv("DOWNLOAD_TEMP_DIR", "data/temp"))
    temp_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = temp_dir / f"tg_{uuid.uuid4().hex}"

    try:
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(str(tmp_path))

        with tmp_path.open("rb") as f:
            entities = _decode_entities(caption_entities)
            parse_mode = None if entities else _to_parse_mode(caption_parse_mode)
            if media_type == "photo":
                await bot.send_photo(
                    chat_id=telegram_channel_id,
                    photo=f,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            elif media_type == "video":
                await bot.send_video(
                    chat_id=telegram_channel_id,
                    video=f,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            elif media_type == "document":
                await bot.send_document(
                    chat_id=telegram_channel_id,
                    document=f,
                    caption=caption,
                    parse_mode=parse_mode,
                    caption_entities=entities,
                )
            else:
                return False

        logger.info("Recovered by downloading and re-uploading media_type=%s", media_type)
        return True
    except Exception as e:
        logger.error("Download fallback failed for media_type=%s: %s", media_type, e, exc_info=True)
        return False
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _resolve_file_ref(*, file_id: str | None, file_path: str | None) -> str | Path:
    if file_id:
        return file_id
    if file_path:
        return Path(file_path)
    raise ValueError("No file_id or file_path available")


def _parse_media_group(
    media_group_data: str,
    *,
    stack: ExitStack,
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """Parse media_group_data JSON into InputMedia objects.

    Expected format: list of dicts with keys:
    - media_type: "photo" | "video" | "document"
    - file_id or file_path
    - caption (optional; only first item should have caption)
    - caption_parse_mode (optional): NULL (plain), 'markdownv2', or 'html'
    - caption_entities (optional): JSON list or list of Telegram MessageEntity dicts
    """
    items = json.loads(media_group_data)
    if not isinstance(items, list) or not items:
        raise ValueError("media_group_data must be a non-empty list")

    media: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("media_group_data items must be objects")

        media_type = item.get("media_type")
        caption = item.get("caption")
        caption_parse_mode = item.get("caption_parse_mode")
        caption_entities = item.get("caption_entities")
        payload = _resolve_file_ref(file_id=item.get("file_id"), file_path=item.get("file_path"))
        if isinstance(payload, Path):
            payload = stack.enter_context(payload.open("rb"))

        entities = _decode_entities(caption_entities)
        parse_mode = None if entities else _to_parse_mode(caption_parse_mode)

        match media_type:
            case "photo":
                media.append(
                    InputMediaPhoto(media=payload, caption=caption, parse_mode=parse_mode, caption_entities=entities)
                )
            case "video":
                media.append(
                    InputMediaVideo(media=payload, caption=caption, parse_mode=parse_mode, caption_entities=entities)
                )
            case "document":
                media.append(
                    InputMediaDocument(media=payload, caption=caption, parse_mode=parse_mode, caption_entities=entities)
                )
            case _:
                raise ValueError(f"Unsupported media_type in media group: {media_type}")

    return media


def _to_parse_mode(value: str | None) -> str | None:
    if value == "markdownv2":
        return ParseMode.MARKDOWN_V2
    if value == "html":
        return ParseMode.HTML
    return None


def _decode_entities(value: Any) -> list[MessageEntity] | None:
    """Decode caption entities from DB/JSON into MessageEntity objects."""
    if value is None:
        return None

    data: Any
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except Exception:
            return None
    else:
        data = value

    if not isinstance(data, list) or not data:
        return None

    entities: list[MessageEntity] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        try:
            entities.append(MessageEntity(**raw))
        except Exception:
            try:
                entities.append(MessageEntity.de_json(raw, bot=None))  # type: ignore[arg-type]
            except Exception:
                continue

    return entities or None

