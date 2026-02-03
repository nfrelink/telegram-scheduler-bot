"""User-scoped DB access helpers with ownership checks.

These helpers wrap low-level query functions with ownership checks so handlers
don’t accidentally operate on another user’s data.
"""

from __future__ import annotations

from typing import Any

from . import queries as q


async def get_channel_by_telegram_id_for_user(user_id: int, telegram_channel_id: str) -> dict[str, Any] | None:
    channel = await q.get_channel_by_telegram_id(telegram_channel_id)
    if channel is None or int(channel["user_id"]) != int(user_id):
        return None
    return channel


async def get_channel_by_id_for_user(user_id: int, channel_db_id: int) -> dict[str, Any] | None:
    channel = await q.get_channel_by_id(channel_db_id)
    if channel is None or int(channel["user_id"]) != int(user_id):
        return None
    return channel

