"""Shared handler utilities."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from database import queries as db

logger = logging.getLogger(__name__)


def get_admin_user_id() -> int | None:
    """Get the configured admin user id, if present."""
    raw = os.getenv("ADMIN_USER_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not an integer")
        return None


async def ensure_user_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Upsert the current user and mark last_active_at."""
    user = update.effective_user
    if user is None:
        return {}

    admin_user_id = get_admin_user_id()
    is_admin = admin_user_id is not None and user.id == admin_user_id

    return await db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_admin=is_admin,
    )


if TYPE_CHECKING:  # pragma: no cover
    # For type-checkers only; avoids unused import warnings at runtime.
    _ = ContextTypes.DEFAULT_TYPE

