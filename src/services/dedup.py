"""Duplicate detection service.

Wraps the per-channel scanning toggle, the per-user alerts toggle, and the
two-layer match policy (exact `file_unique_id` + perceptual dHash). Handlers
that warn the uploader about repeats go through this module so the policy
lives in one place.

dHash *computation* lives in `utils.image_hash.compute_dhash` because it
needs the Telegram bot to fetch the file. This module accepts an
already-computed dhash value and only handles the database-side lookup +
hamming comparison.
"""

from __future__ import annotations

import logging
from typing import Any

from database import queries as db
from utils.image_hash import find_similar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def is_channel_scanning_enabled(channel_db_id: int) -> bool:
    """True when the channel owner has switched on duplicate scanning."""
    return await db.get_channel_duplicate_detection(channel_db_id)


async def set_channel_scanning_enabled(channel_db_id: int, *, enabled: bool) -> None:
    await db.set_channel_duplicate_detection(channel_db_id, enabled=enabled)


async def is_user_alerts_enabled(user_id: int) -> bool:
    """True when the user wants to be warned about probable repeats."""
    return await db.get_user_duplicate_alerts(user_id)


async def set_user_alerts_enabled(user_id: int, *, enabled: bool) -> None:
    await db.set_user_duplicate_alerts(user_id, enabled=enabled)


async def should_check(*, channel_db_id: int, user_id: int) -> bool:
    """True only when both the channel and the user opt in.

    Either gate disables the check entirely; callers can short-circuit when
    this returns False (still recording the fingerprint with `dhash=None`
    so future scans have something to compare against if the channel toggle
    is later flipped on).
    """
    if not await is_channel_scanning_enabled(channel_db_id):
        return False
    if not await is_user_alerts_enabled(user_id):
        return False
    return True


# ---------------------------------------------------------------------------
# Match lookup
# ---------------------------------------------------------------------------

async def find_by_file_unique_id(
    channel_db_id: int, file_unique_id: str
) -> dict[str, Any] | None:
    """Layer 1: exact file_unique_id hit. Cheapest possible check; works
    for every media type (photo / video / document / etc)."""
    return await db.find_fingerprint_by_file_unique_id(channel_db_id, file_unique_id)


async def find_by_dhash(
    channel_db_id: int, dhash_value: int
) -> dict[str, Any] | None:
    """Layer 2: perceptual hash search. Photos only (others have no dHash).

    Loads every existing fingerprint's dhash for the channel and returns the
    first one within the configured Hamming threshold. The two-step
    `find_similar` → `get_fingerprint` shape mirrors the previous direct-DB
    calls in `bulk_upload`.
    """
    existing = await db.get_channel_dhashes(channel_db_id)
    match_id = find_similar(dhash_value, existing)
    if match_id is None:
        return None
    return await db.get_fingerprint(match_id)
