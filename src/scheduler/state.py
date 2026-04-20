"""Schedule state derivations.

A thin bridge between the database layer (queries) and the timing layer
(`calculate_next_run`). The scheduler tick and any handler that mutates a
schedule's state (pattern edit, timezone edit, pause, resume) calls
`recompute_next_run` to keep `schedules.next_planned_run_at` consistent.

This module is intentionally narrow; it gets folded into the services layer
introduced in Phase 1.3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import queries as db
from scheduler.timing import calculate_next_run

logger = logging.getLogger(__name__)


# States in which next_planned_run_at carries a meaningful value. Any other
# state (paused, empty_paused) clears the column.
_ACTIVE_STATES: frozenset[str] = frozenset({"active"})


async def recompute_next_run(
    schedule_id: int, *, now: datetime | None = None
) -> datetime | None:
    """Recompute `schedules.next_planned_run_at` for one schedule and persist it.

    Returns the new value (or None if the schedule is paused/missing/has an
    invalid pattern, in which case the column is cleared).

    Idempotent: safe to call after any state-changing op. Callers do not need
    to check the schedule's state first.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    schedule = await db.get_schedule(schedule_id)
    if schedule is None:
        return None

    next_at = _derive_next(schedule, now=now)
    await db.update_schedule_next_planned_run(schedule_id, next_at)
    return next_at


def _derive_next(schedule: dict, *, now: datetime) -> datetime | None:
    """Return calculate_next_run(after=now) for active schedules, else None.

    An invalid pattern returns None too; the engine pauses such schedules on
    its next tick (see `_process_schedule`'s validation branch), which then
    triggers a recompute that confirms the cleared value.
    """
    state = str(schedule.get("state") or "")
    if state not in _ACTIVE_STATES:
        return None
    try:
        return calculate_next_run(schedule, after=now)
    except ValueError as e:
        logger.warning(
            "Cannot compute next run for schedule id=%s (invalid pattern): %s",
            schedule.get("id"),
            e,
        )
        return None
