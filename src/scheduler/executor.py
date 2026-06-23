"""Post execution logic (send queued posts to channels)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo, MessageEntity
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ExtBot

logger = logging.getLogger(__name__)

_FILE_ID_ERROR_RE = re.compile(r"(file[_ ]?id|file identifier|wrong file)", re.IGNORECASE)

# Maps media_type -> (bot method name, media keyword argument name)
_SINGLE_SEND: dict[str, tuple[str, str]] = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "document": ("send_document", "document"),
}

# Maps media_type -> InputMedia class for media groups
_INPUT_MEDIA: dict[str, type] = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
}


class _PermanentSendError(Exception):
    """Data integrity error — retrying will never succeed."""


async def send_post(
    bot: ExtBot, *, telegram_channel_id: str, post: dict[str, Any]
) -> tuple[bool, str | None, bool]:
    """Send a queued post to a Telegram channel.

    Returns `(True, None, True)` on success, `(False, error_text, retryable)` on
    failure. `retryable=False` signals a permanent data integrity error; the
    caller should skip retry scheduling. `error_text` is
    `f"{type(e).__name__}: {e}"` of the exception that finally killed the
    attempt (after the file_id-error retry fallback, if applicable).

    Supported media_type:
    - photo
    - video
    - document
    - media_group
    """
    forward_from_chat_id = post.get("forward_from_chat_id")
    forward_from_message_id = post.get("forward_from_message_id")

    if forward_from_chat_id is not None and forward_from_message_id is not None:
        try:
            await bot.forward_message(
                chat_id=telegram_channel_id,
                from_chat_id=int(forward_from_chat_id),
                message_id=int(forward_from_message_id),
            )
        except Exception as e:
            logger.exception(
                "Failed to forward post id=%s to channel=%s",
                post.get("id"),
                telegram_channel_id,
                extra={
                    "event": "forward_failed",
                    "post_id": post.get("id"),
                    "channel_id": telegram_channel_id,
                    "from_chat_id": forward_from_chat_id,
                    "from_message_id": forward_from_message_id,
                },
            )
            return False, _format_error(e), True
        else:
            return True, None, True

    media_type = post.get("media_type")
    caption = post.get("caption")
    caption_parse_mode = post.get("caption_parse_mode")
    caption_entities = post.get("caption_entities")

    file_id = post.get("file_id")
    file_path = post.get("file_path")

    try:
        await _send_post_once(
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
    except _PermanentSendError as e:
        logger.exception(
            "Failed to send post id=%s to channel=%s",
            post.get("id"),
            telegram_channel_id,
            extra={
                "event": "send_failed",
                "post_id": post.get("id"),
                "channel_id": telegram_channel_id,
                "media_type": media_type,
            },
        )
        return False, _format_error(e), False
    except Exception as e:
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
                return True, None, True

        logger.exception(
            "Failed to send post id=%s to channel=%s",
            post.get("id"),
            telegram_channel_id,
            extra={
                "event": "send_failed",
                "post_id": post.get("id"),
                "channel_id": telegram_channel_id,
                "media_type": media_type,
            },
        )
        return False, _format_error(e), True
    else:
        return True, None, True


def _format_error(exc: Exception) -> str:
    """Render `exc` as `'Type: message'` for admin DMs.

    Kept short and predictable so two distinct failures with the same
    type+message still collide on the admin debounce key (per-schedule
    scoping is what avoids cross-schedule suppression).
    """
    msg = str(exc).strip() or "(no message)"
    return f"{type(exc).__name__}: {msg}"


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

    if media_type in _SINGLE_SEND:
        method_name, media_kwarg = _SINGLE_SEND[media_type]
        method = getattr(bot, method_name)
        payload = _resolve_file_ref(file_id=file_id, file_path=file_path)
        kwargs = {
            "chat_id": telegram_channel_id,
            "caption": caption,
            "parse_mode": parse_mode,
            "caption_entities": entities,
        }
        if isinstance(payload, Path):
            with payload.open("rb") as f:
                await method(**{media_kwarg: f}, **kwargs)
        else:
            await method(**{media_kwarg: payload}, **kwargs)
        return True

    match media_type:
        case "media_group":
            media_group_data = post.get("media_group_data")
            if not media_group_data:
                raise _PermanentSendError("media_group_data missing")

            forward_refs = _parse_media_group_forward_refs(media_group_data)
            if forward_refs is not None:
                from_chat_id, message_ids = forward_refs
                # Use forward_messages so Telegram can preserve grouping/attribution
                # as much as possible.
                result = await bot.forward_messages(
                    chat_id=telegram_channel_id,
                    from_chat_id=from_chat_id,
                    message_ids=message_ids,
                )
                if len(result) != len(message_ids):
                    raise RuntimeError("forward_messages returned fewer results than requested")
                return True

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

        if media_type not in _SINGLE_SEND:
            return False
        with tmp_path.open("rb") as f:
            entities = _decode_entities(caption_entities)
            parse_mode = None if entities else _to_parse_mode(caption_parse_mode)
            method_name, media_kwarg = _SINGLE_SEND[media_type]
            await getattr(bot, method_name)(
                **{media_kwarg: f},
                chat_id=telegram_channel_id,
                caption=caption,
                parse_mode=parse_mode,
                caption_entities=entities,
            )

        logger.info(
            "Recovered by downloading and re-uploading media_type=%s",
            media_type,
            extra={"event": "send_recovered_via_download", "media_type": media_type},
        )
    except Exception:
        logger.exception(
            "Download fallback failed for media_type=%s",
            media_type,
            extra={"event": "download_fallback_failed", "media_type": media_type},
        )
        return False
    else:
        return True
    finally:
        with suppress(Exception):
            tmp_path.unlink(missing_ok=True)


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
            raise TypeError("media_group_data items must be objects")

        media_type = item.get("media_type")
        caption = item.get("caption")
        caption_parse_mode = item.get("caption_parse_mode")
        caption_entities = item.get("caption_entities")
        payload = _resolve_file_ref(file_id=item.get("file_id"), file_path=item.get("file_path"))
        if isinstance(payload, Path):
            payload = stack.enter_context(payload.open("rb"))

        entities = _decode_entities(caption_entities)
        parse_mode = None if entities else _to_parse_mode(caption_parse_mode)

        media_cls = _INPUT_MEDIA.get(media_type)
        if media_cls is None:
            raise ValueError(f"Unsupported media_type in media group: {media_type}")
        media.append(
            media_cls(
                media=payload,
                caption=caption,
                parse_mode=parse_mode,
                caption_entities=entities,
            )
        )

    return media


def _parse_media_group_forward_refs(
    media_group_data: str,
) -> tuple[int, list[int]] | None:
    """Parse media_group_data for forwarding metadata.

    Returns (from_chat_id, message_ids) if every item has:
    - forward_from_chat_id
    - forward_from_message_id

    Otherwise returns None (meaning: send as a copied media group).
    """
    pairs = _collect_media_group_forward_pairs(media_group_data)
    if pairs is None:
        return None

    base_from = pairs[0][0]
    if any(fc != base_from for (fc, _mid) in pairs):
        return None

    # Stable order for forwarding: ascending by message_id in the source chat.
    message_ids = [mid for (_fc, mid) in sorted(pairs, key=lambda p: p[1])]
    return base_from, message_ids


def _collect_media_group_forward_pairs(
    media_group_data: str,
) -> list[tuple[int, int]] | None:
    try:
        items = json.loads(media_group_data)
    except Exception:
        return None

    if not isinstance(items, list) or not items:
        return None

    pairs: list[tuple[int, int]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        from_chat_id = item.get("forward_from_chat_id")
        message_id = item.get("forward_from_message_id")
        if from_chat_id is None or message_id is None:
            return None
        try:
            pairs.append((int(from_chat_id), int(message_id)))
        except TypeError, ValueError:
            return None

    return pairs


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
                logger.debug("Skipping invalid caption entity payload: %r", raw, exc_info=True)
                continue

    return entities or None
