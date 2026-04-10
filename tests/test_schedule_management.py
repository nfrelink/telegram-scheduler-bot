"""Tests for schedule_management wizard state transitions.

Covers the new-schedule and edit-schedule wizard flows,
plus the pure input-parsing helpers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers.schedule_management import (
    ES_WAIT_DAILY_TIMES,
    ES_WAIT_FIELD,
    ES_WAIT_INTERVAL,
    ES_WAIT_NAME,
    ES_WAIT_TYPE,
    ES_WAIT_WEEKLY_DAYS,
    NS_WAIT_DAILY_TIMES,
    NS_WAIT_INTERVAL,
    NS_WAIT_NAME,
    NS_WAIT_TYPE,
    NS_WAIT_WEEKLY_DAYS,
    NS_WAIT_WEEKLY_TIMES,
    _parse_interval_input,
    _parse_times_csv,
    _parse_weekdays_csv,
    _pattern_summary,
    editschedule_choose_field,
    editschedule_set_type,
    newschedule_set_name,
    newschedule_set_type,
    newschedule_set_interval,
    newschedule_set_daily_times,
    newschedule_set_weekly_days,
    newschedule_set_weekly_times,
)
from telegram.ext import ConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_context(**user_data_init) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = dict(user_data_init)
    ctx.bot = AsyncMock()
    ctx.args = []
    return ctx


def _mock_update(*, text: str | None = None, user_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()

    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock()
    update.message = message
    update.callback_query = None
    return update


# ===========================================================================
# Pure input parsers
# ===========================================================================


class TestParseIntervalInput:
    def test_hours(self) -> None:
        assert _parse_interval_input("2h") == (2, 0)

    def test_minutes(self) -> None:
        assert _parse_interval_input("30m") == (0, 30)

    def test_bare_number_is_minutes(self) -> None:
        assert _parse_interval_input("90") == (0, 90)

    def test_zero_returns_none(self) -> None:
        assert _parse_interval_input("0h") is None
        assert _parse_interval_input("0m") is None
        assert _parse_interval_input("0") is None

    def test_negative_returns_none(self) -> None:
        assert _parse_interval_input("-1h") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_interval_input("") is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_interval_input("abc") is None

    def test_whitespace_stripped(self) -> None:
        assert _parse_interval_input("  1h  ") == (1, 0)


class TestParseTimesCsv:
    def test_single_time(self) -> None:
        result = _parse_times_csv("09:00")
        assert result == ["09:00"]

    def test_multiple_times(self) -> None:
        result = _parse_times_csv("09:00, 16:30")
        assert result == ["09:00", "16:30"]

    def test_invalid_time(self) -> None:
        assert _parse_times_csv("25:00") is None
        assert _parse_times_csv("abc") is None

    def test_empty(self) -> None:
        assert _parse_times_csv("") is None

    def test_normalizes_format(self) -> None:
        result = _parse_times_csv("9:5")
        assert result == ["09:05"]


class TestParseWeekdaysCsv:
    def test_valid_days(self) -> None:
        result = _parse_weekdays_csv("monday, wednesday, friday")
        assert result == ["monday", "wednesday", "friday"]

    def test_deduplicates(self) -> None:
        result = _parse_weekdays_csv("monday, monday, tuesday")
        assert result == ["monday", "tuesday"]

    def test_invalid_day(self) -> None:
        assert _parse_weekdays_csv("notaday") is None

    def test_empty(self) -> None:
        assert _parse_weekdays_csv("") is None

    def test_case_insensitive(self) -> None:
        result = _parse_weekdays_csv("Monday, TUESDAY")
        assert result == ["monday", "tuesday"]


class TestPatternSummary:
    def test_interval(self) -> None:
        s = _pattern_summary({"type": "interval", "hours": 2, "minutes": 30})
        assert "2h 30m" in s

    def test_daily(self) -> None:
        s = _pattern_summary({"type": "daily", "times": ["09:00", "16:00"]}, tz_name="CET")
        assert "daily" in s
        assert "CET" in s

    def test_weekly(self) -> None:
        s = _pattern_summary(
            {"type": "weekly", "days": ["monday"], "times": ["12:00"]}, tz_name="UTC"
        )
        assert "weekly" in s
        assert "monday" in s


# ===========================================================================
# New-schedule wizard — state transitions
# ===========================================================================


class TestNewScheduleSetName:
    @pytest.mark.asyncio
    async def test_valid_name_goes_to_wait_type(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="My Schedule")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_name(update, ctx)
        assert result == NS_WAIT_TYPE
        assert ctx.user_data["ns_name"] == "My Schedule"

    @pytest.mark.asyncio
    async def test_empty_name_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_name(update, ctx)
        assert result == NS_WAIT_NAME


class TestNewScheduleSetType:
    @pytest.mark.asyncio
    async def test_interval_goes_to_wait_interval(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="interval")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_type(update, ctx)
        assert result == NS_WAIT_INTERVAL
        assert ctx.user_data["ns_type"] == "interval"

    @pytest.mark.asyncio
    async def test_daily_goes_to_wait_daily_times(self) -> None:
        ctx = _mock_context(ns_timezone="UTC")
        update = _mock_update(text="daily")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_type(update, ctx)
        assert result == NS_WAIT_DAILY_TIMES

    @pytest.mark.asyncio
    async def test_weekly_goes_to_wait_weekly_days(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="weekly")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_type(update, ctx)
        assert result == NS_WAIT_WEEKLY_DAYS

    @pytest.mark.asyncio
    async def test_invalid_type_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="biweekly")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_type(update, ctx)
        assert result == NS_WAIT_TYPE

    @pytest.mark.asyncio
    async def test_case_insensitive(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="DAILY")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_type(update, ctx)
        assert result == NS_WAIT_DAILY_TIMES


class TestNewScheduleSetInterval:
    @pytest.mark.asyncio
    async def test_valid_interval_finalizes(self) -> None:
        ctx = _mock_context(ns_channel_db_id=1, ns_name="Test", ns_timezone="UTC")
        update = _mock_update(text="2h")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.schedule_management.db") as mock_db:
                mock_db.create_schedule = AsyncMock(return_value={"id": 1})
                mock_db.set_user_context = AsyncMock()
                mock_db.get_user_context = AsyncMock(return_value={})
                result = await newschedule_set_interval(update, ctx)
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_invalid_interval_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="abc")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_interval(update, ctx)
        assert result == NS_WAIT_INTERVAL


class TestNewScheduleSetDailyTimes:
    @pytest.mark.asyncio
    async def test_valid_times_finalizes(self) -> None:
        ctx = _mock_context(ns_channel_db_id=1, ns_name="Daily", ns_timezone="UTC")
        update = _mock_update(text="09:00,16:00")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.schedule_management.db") as mock_db:
                mock_db.create_schedule = AsyncMock(return_value={"id": 2})
                mock_db.set_user_context = AsyncMock()
                mock_db.get_user_context = AsyncMock(return_value={})
                result = await newschedule_set_daily_times(update, ctx)
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_invalid_times_stays(self) -> None:
        ctx = _mock_context(ns_timezone="UTC")
        update = _mock_update(text="99:99")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_daily_times(update, ctx)
        assert result == NS_WAIT_DAILY_TIMES


class TestNewScheduleSetWeeklyDays:
    @pytest.mark.asyncio
    async def test_valid_days_goes_to_weekly_times(self) -> None:
        ctx = _mock_context(ns_timezone="UTC")
        update = _mock_update(text="monday,friday")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_weekly_days(update, ctx)
        assert result == NS_WAIT_WEEKLY_TIMES
        assert ctx.user_data["ns_days"] == ["monday", "friday"]

    @pytest.mark.asyncio
    async def test_invalid_days_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="notaday")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_weekly_days(update, ctx)
        assert result == NS_WAIT_WEEKLY_DAYS


class TestNewScheduleSetWeeklyTimes:
    @pytest.mark.asyncio
    async def test_valid_times_finalizes(self) -> None:
        ctx = _mock_context(
            ns_channel_db_id=1, ns_name="Weekly", ns_timezone="UTC",
            ns_days=["monday"],
        )
        update = _mock_update(text="12:00")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            with patch("handlers.schedule_management.db") as mock_db:
                mock_db.create_schedule = AsyncMock(return_value={"id": 3})
                mock_db.set_user_context = AsyncMock()
                mock_db.get_user_context = AsyncMock(return_value={})
                result = await newschedule_set_weekly_times(update, ctx)
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_invalid_times_stays(self) -> None:
        ctx = _mock_context(ns_timezone="UTC")
        update = _mock_update(text="nope")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await newschedule_set_weekly_times(update, ctx)
        assert result == NS_WAIT_WEEKLY_TIMES


# ===========================================================================
# Edit-schedule wizard — state transitions
# ===========================================================================


class TestEditScheduleChooseField:
    @pytest.mark.asyncio
    async def test_name_goes_to_wait_name(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="name")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_choose_field(update, ctx)
        assert result == ES_WAIT_NAME

    @pytest.mark.asyncio
    async def test_pattern_goes_to_wait_type(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="pattern")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_choose_field(update, ctx)
        assert result == ES_WAIT_TYPE

    @pytest.mark.asyncio
    async def test_invalid_stays(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="something")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_choose_field(update, ctx)
        assert result == ES_WAIT_FIELD


class TestEditScheduleSetType:
    @pytest.mark.asyncio
    async def test_interval(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="interval")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_set_type(update, ctx)
        assert result == ES_WAIT_INTERVAL

    @pytest.mark.asyncio
    async def test_daily(self) -> None:
        ctx = _mock_context(es_timezone="UTC")
        update = _mock_update(text="daily")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_set_type(update, ctx)
        assert result == ES_WAIT_DAILY_TIMES

    @pytest.mark.asyncio
    async def test_weekly(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="weekly")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_set_type(update, ctx)
        assert result == ES_WAIT_WEEKLY_DAYS

    @pytest.mark.asyncio
    async def test_invalid(self) -> None:
        ctx = _mock_context()
        update = _mock_update(text="nope")
        with patch("handlers.schedule_management.ensure_user_record", new_callable=AsyncMock):
            result = await editschedule_set_type(update, ctx)
        assert result == ES_WAIT_TYPE
