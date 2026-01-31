from __future__ import annotations

import pytest
from telegram.error import BadRequest

from scheduler import executor


@pytest.mark.asyncio
async def test_send_post_triggers_download_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"retry": 0}

    async def fake_send_once(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise BadRequest("Bad Request: wrong file_id specified")

    async def fake_retry(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        called["retry"] += 1
        return True

    monkeypatch.setattr(executor, "_send_post_once", fake_send_once)
    monkeypatch.setattr(executor, "_retry_with_download", fake_retry)

    bot = object()
    post = {"id": 1, "media_type": "photo", "file_id": "abc", "file_path": None, "caption": None}
    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is True
    assert called["retry"] == 1


@pytest.mark.asyncio
async def test_send_post_does_not_retry_on_non_file_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send_once(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise BadRequest("Bad Request: chat not found")

    async def fake_retry(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        raise AssertionError("fallback retry should not be called")

    monkeypatch.setattr(executor, "_send_post_once", fake_send_once)
    monkeypatch.setattr(executor, "_retry_with_download", fake_retry)

    bot = object()
    post = {"id": 2, "media_type": "photo", "file_id": "abc", "file_path": None, "caption": None}
    ok = await executor.send_post(bot, telegram_channel_id="-1", post=post)  # type: ignore[arg-type]
    assert ok is False

