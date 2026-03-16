"""Tests for src/handlers/timezone_management.py — Phase 5."""
from __future__ import annotations

import pytest

from database import queries as db
from handlers.timezone_management import (
    _is_valid_timezone,
    _region_keyboard,
    _regions_keyboard,
    timezone_callback,
)


# ---------------------------------------------------------------------------
# _is_valid_timezone
# ---------------------------------------------------------------------------

def test_is_valid_timezone_utc() -> None:
    assert _is_valid_timezone("UTC") is True


def test_is_valid_timezone_known_iana_names() -> None:
    assert _is_valid_timezone("Europe/Amsterdam") is True
    assert _is_valid_timezone("Asia/Tokyo") is True
    assert _is_valid_timezone("America/New_York") is True
    assert _is_valid_timezone("Australia/Sydney") is True


def test_is_valid_timezone_invalid_strings() -> None:
    assert _is_valid_timezone("Not/A/Timezone") is False
    assert _is_valid_timezone("garbage") is False
    assert _is_valid_timezone("") is False


# ---------------------------------------------------------------------------
# _regions_keyboard
# ---------------------------------------------------------------------------

def test_regions_keyboard_row_count() -> None:
    # 5 region rows + 1 "Enter manually" row = 6 rows total.
    kb = _regions_keyboard()
    assert len(kb.inline_keyboard) == 6


def test_regions_keyboard_region_buttons_have_correct_prefix() -> None:
    kb = _regions_keyboard()
    region_rows = kb.inline_keyboard[:-1]  # all except the last (manual entry) row
    for row in region_rows:
        assert row[0].callback_data.startswith("tz:r:")


def test_regions_keyboard_last_button_is_manual_entry() -> None:
    kb = _regions_keyboard()
    last_button = kb.inline_keyboard[-1][0]
    assert last_button.callback_data == "tz:manual"


# ---------------------------------------------------------------------------
# _region_keyboard
# ---------------------------------------------------------------------------

def test_region_keyboard_known_region_returns_keyboard() -> None:
    for key in ("africa", "americas", "asia", "europe", "utc"):
        assert _region_keyboard(key) is not None


def test_region_keyboard_unknown_region_returns_none() -> None:
    assert _region_keyboard("mars") is None
    assert _region_keyboard("") is None


def test_region_keyboard_has_back_and_manual_buttons() -> None:
    kb = _region_keyboard("europe")
    assert kb is not None
    all_datas = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    assert "tz:regions" in all_datas
    assert "tz:manual" in all_datas


def test_region_keyboard_timezone_buttons_all_point_to_valid_iana() -> None:
    for key in ("africa", "americas", "asia", "europe", "utc"):
        kb = _region_keyboard(key)
        assert kb is not None
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data.startswith("tz:s:"):
                    iana = btn.callback_data[len("tz:s:"):]
                    assert _is_valid_timezone(iana), f"Invalid IANA name in region {key!r}: {iana!r}"


def test_region_keyboard_uses_two_buttons_per_row_for_timezone_entries() -> None:
    # Europe has 8 timezone entries, so the first rows should be pairs.
    kb = _region_keyboard("europe")
    assert kb is not None
    tz_rows = [row for row in kb.inline_keyboard if any(b.callback_data.startswith("tz:s:") for b in row)]
    for row in tz_rows:
        assert len(row) in (1, 2)


# ---------------------------------------------------------------------------
# timezone_callback — lightweight fake-update tests
# ---------------------------------------------------------------------------

class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.username = "u"
        self.first_name = "f"
        self.last_name = "l"


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self._answered = False
        self._edited_text: str | None = None
        self._edited_markup = None

    async def answer(self) -> None:
        self._answered = True

    async def edit_message_text(self, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._edited_text = text
        self._edited_markup = kwargs.get("reply_markup")


class _FakeUpdate:
    def __init__(self, *, data: str, user_id: int) -> None:
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_user = _FakeUser(user_id)


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}


@pytest.mark.asyncio
async def test_timezone_callback_set_valid_tz_updates_db(initialized_db) -> None:
    user_id = 7001
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:s:Europe/Amsterdam", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "Europe/Amsterdam" in (update.callback_query._edited_text or "")
    assert await db.get_user_timezone(user_id) == "Europe/Amsterdam"


@pytest.mark.asyncio
async def test_timezone_callback_set_invalid_tz_does_not_update_db(initialized_db) -> None:
    user_id = 7002
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:s:Not/Real/Zone", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "Invalid" in (update.callback_query._edited_text or "")
    assert await db.get_user_timezone(user_id) is None


@pytest.mark.asyncio
async def test_timezone_callback_region_drill_edits_message(initialized_db) -> None:
    user_id = 7003
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:r:europe", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "Europe" in (update.callback_query._edited_text or "")
    assert update.callback_query._edited_markup is not None


@pytest.mark.asyncio
async def test_timezone_callback_region_unknown_key(initialized_db) -> None:
    user_id = 7004
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:r:nonexistent", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "Unknown region" in (update.callback_query._edited_text or "")


@pytest.mark.asyncio
async def test_timezone_callback_regions_back_shows_region_list(initialized_db) -> None:
    user_id = 7005
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:regions", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "region" in (update.callback_query._edited_text or "").lower()
    assert update.callback_query._edited_markup is not None


@pytest.mark.asyncio
async def test_timezone_callback_manual_shows_settimezone_instructions(initialized_db) -> None:
    user_id = 7006
    await db.upsert_user(user_id=user_id, username="u", first_name="f", last_name="l", is_admin=False)

    update = _FakeUpdate(data="tz:manual", user_id=user_id)
    await timezone_callback(update, _FakeContext())  # type: ignore[arg-type]

    assert update.callback_query._answered
    assert "/settimezone" in (update.callback_query._edited_text or "")
