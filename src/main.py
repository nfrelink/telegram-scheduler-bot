#!/usr/bin/env python3
"""Main entry point for Telegram Scheduler Bot."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import signal
import sys
import warnings
from typing import Final

# The filter must be installed before importing python-telegram-bot
# (transitively pulled in via `from bot import ...`); otherwise the
# per_message=False UserWarning fires at import time before the filter is
# active. The deferred imports below are intentional — do not reorder.
warnings.filterwarnings("ignore", message=r".*per_message=False.*", category=UserWarning)

from dotenv import load_dotenv  # noqa: E402

from bot import create_application, register_commands  # noqa: E402
from database import init_database  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from scheduler import start_scheduler  # noqa: E402

logger = logging.getLogger(__name__)


def _require_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def main() -> None:
    """Start the bot (polling)."""
    load_dotenv()
    setup_logging()

    _require_env_var("TELEGRAM_BOT_TOKEN")
    _require_env_var("ADMIN_USER_ID")

    await init_database()
    logger.info("Database initialised", extra={"event": "database_initialised"})

    application = create_application()

    shutdown: Final[asyncio.Event] = asyncio.Event()

    def _handle_shutdown(sig: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info(
            "Received signal %s, initiating shutdown",
            sig,
            extra={"event": "shutdown_signal", "signal": sig},
        )
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    await application.initialize()
    await register_commands(application)
    await application.start()
    scheduler_task = asyncio.create_task(start_scheduler(application.bot))
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot polling started", extra={"event": "bot_polling_started"})

    try:
        await shutdown.wait()
    finally:
        logger.info("Stopping bot", extra={"event": "bot_stopping"})
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Bot shutdown complete", extra={"event": "bot_shutdown_complete"})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Basic last-resort logging; logging may not be configured yet.
        print(f"Fatal error: {e}", file=sys.stderr)
        raise

