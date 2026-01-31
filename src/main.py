#!/usr/bin/env python3
"""Main entry point for Telegram Scheduler Bot."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import re
import signal
import sys
from typing import Final

from dotenv import load_dotenv

from bot import create_application
from database import init_database
from scheduler import start_scheduler

logger = logging.getLogger(__name__)


class _RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts Telegram bot tokens in output."""

    # Matches: bot<digits>:<token>
    _BOT_PREFIX_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")

    # Matches: <digits>:<token> (token-like strings)
    _TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = self._BOT_PREFIX_TOKEN_RE.sub("bot<redacted>", text)
        text = self._TOKEN_RE.sub("<redacted>", text)
        return text


def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Security + noise reduction:
    # httpx logs request URLs (which contain the bot token), so keep it quiet by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _require_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def main() -> None:
    """Start the bot (polling)."""
    load_dotenv()
    _configure_logging()

    _require_env_var("TELEGRAM_BOT_TOKEN")
    _require_env_var("ADMIN_USER_ID")

    logger.info("Initializing database...")
    await init_database()
    logger.info("Database initialized")

    application = create_application()

    shutdown: Final[asyncio.Event] = asyncio.Event()

    def _handle_shutdown(sig: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info("Received signal %s, initiating shutdown...", sig)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info("Starting bot polling...")
    await application.initialize()
    await application.start()
    scheduler_task = asyncio.create_task(start_scheduler(application.bot))
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        await shutdown.wait()
    finally:
        logger.info("Stopping bot...")
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Basic last-resort logging; logging may not be configured yet.
        print(f"Fatal error: {e}", file=sys.stderr)
        raise

