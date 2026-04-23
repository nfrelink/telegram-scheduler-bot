"""Tests for services.dedup — settings toggles + match lookup."""

from __future__ import annotations

import pytest

from database import queries as db
from services import dedup


async def _mk_channel(user_id: int, suffix: str) -> int:
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    ch = await db.create_channel(
        user_id=user_id,
        telegram_channel_id=f"-100{suffix}",
        channel_name=f"C-{suffix}",
    )
    return int(ch["id"])


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_scanning_toggle_roundtrip(initialized_db) -> None:
    user_id = 7500
    ch_id = await _mk_channel(user_id, "500")
    assert await dedup.is_channel_scanning_enabled(ch_id) is False
    await dedup.set_channel_scanning_enabled(ch_id, enabled=True)
    assert await dedup.is_channel_scanning_enabled(ch_id) is True
    await dedup.set_channel_scanning_enabled(ch_id, enabled=False)
    assert await dedup.is_channel_scanning_enabled(ch_id) is False


@pytest.mark.asyncio
async def test_user_alerts_toggle_roundtrip(initialized_db) -> None:
    user_id = 7501
    await db.upsert_user(
        user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False
    )
    assert await dedup.is_user_alerts_enabled(user_id) is True
    await dedup.set_user_alerts_enabled(user_id, enabled=False)
    assert await dedup.is_user_alerts_enabled(user_id) is False
    await dedup.set_user_alerts_enabled(user_id, enabled=True)
    assert await dedup.is_user_alerts_enabled(user_id) is True


@pytest.mark.asyncio
async def test_should_check_requires_both_toggles(initialized_db) -> None:
    """Either gate alone disables the entire check; both must be on."""
    user_id = 7502
    ch_id = await _mk_channel(user_id, "502")

    # Default: channel off → should_check False
    assert await dedup.should_check(channel_db_id=ch_id, user_id=user_id) is False

    # Channel on, user on → True
    await dedup.set_channel_scanning_enabled(ch_id, enabled=True)
    assert await dedup.should_check(channel_db_id=ch_id, user_id=user_id) is True

    # Channel on, user off → False (covers the user-toggle short-circuit)
    await dedup.set_user_alerts_enabled(user_id, enabled=False)
    assert await dedup.should_check(channel_db_id=ch_id, user_id=user_id) is False


# ---------------------------------------------------------------------------
# Match lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_file_unique_id_hits_and_misses(initialized_db) -> None:
    user_id = 7503
    ch_id = await _mk_channel(user_id, "503")
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "u-hit",
                "dhash": None,
                "file_id": "f1",
                "media_type": "photo",
                "queued_post_id": None,
            },
        ],
    )
    hit = await dedup.find_by_file_unique_id(ch_id, "u-hit")
    assert hit is not None and hit["file_unique_id"] == "u-hit"
    miss = await dedup.find_by_file_unique_id(ch_id, "nope")
    assert miss is None


@pytest.mark.asyncio
async def test_find_by_dhash_returns_match_when_within_threshold(
    initialized_db,
) -> None:
    user_id = 7504
    ch_id = await _mk_channel(user_id, "504")
    # Seed a known dhash; query with the same value (Hamming distance 0).
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "u-d",
                "dhash": "12345",
                "file_id": "f1",
                "media_type": "photo",
                "queued_post_id": None,
            },
        ],
    )
    match = await dedup.find_by_dhash(ch_id, 12345)
    assert match is not None
    assert match["dhash"] == "12345"


@pytest.mark.asyncio
async def test_find_by_dhash_returns_none_when_no_candidates(initialized_db) -> None:
    user_id = 7505
    ch_id = await _mk_channel(user_id, "505")
    # No fingerprints stored → find_similar gets empty list → returns None.
    out = await dedup.find_by_dhash(ch_id, 0)
    assert out is None


@pytest.mark.asyncio
async def test_find_by_dhash_returns_none_when_above_threshold(initialized_db) -> None:
    """A stored dhash that differs in every bit must not match."""
    user_id = 7506
    ch_id = await _mk_channel(user_id, "506")
    await db.add_fingerprints_bulk(
        ch_id,
        [
            {
                "file_unique_id": "u-far",
                "dhash": str(0),
                "file_id": "f1",
                "media_type": "photo",
                "queued_post_id": None,
            },
        ],
    )
    # All-bits-on 64-bit value vs all-zero value → Hamming distance 64,
    # well above the configured threshold of 10.
    out = await dedup.find_by_dhash(ch_id, (1 << 64) - 1)
    assert out is None
