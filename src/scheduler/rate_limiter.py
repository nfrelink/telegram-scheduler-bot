"""Rate limiting for Telegram API usage."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RateLimiter:
    """Track and enforce a minimum interval between posts per channel."""

    def __init__(self, *, min_interval_seconds: float = 3.0) -> None:
        self._min_interval_seconds = float(min_interval_seconds)
        self._last_post_at: dict[str, datetime | None] = defaultdict(lambda: None)

    async def wait_if_needed(self, telegram_channel_id: str) -> None:
        """Wait if posting too quickly to the same channel."""
        last = self._last_post_at.get(telegram_channel_id)
        now = datetime.now(timezone.utc)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < self._min_interval_seconds:
                wait_time = self._min_interval_seconds - elapsed
                logger.debug(
                    "Rate limit: waiting %.2fs for channel %s",
                    wait_time,
                    telegram_channel_id,
                )
                await asyncio.sleep(wait_time)

        self._last_post_at[telegram_channel_id] = datetime.now(timezone.utc)

