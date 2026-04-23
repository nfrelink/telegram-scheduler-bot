"""Schedule lifecycle service.

Owns the `schedules.next_planned_run_at` invariant ("active schedules carry
a non-NULL next-run, non-active schedules carry NULL") and the legality of
schedule state transitions.

Public API is the only sanctioned way for handlers and the engine to mutate
schedule rows; direct `db.queries` writes against the `schedules` table from
outside this module are an error. Reads (e.g. `get_schedule_for_user`,
`get_channel_schedules`) remain direct queries since they have no
invariants.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database import queries as db
from scheduler.timing import calculate_next_run
from utils.tz import InvalidTimezoneError, is_valid_timezone, suggest_timezones

logger = logging.getLogger(__name__)

# Re-export so handlers can `except scheduling.InvalidTimezoneError` without
# depending on utils.tz directly.
__all__ = [
    "InvalidTimezoneError",
    "create",
    "delete",
    "mark_empty",
    "next_planned_for",
    "pause",
    "persist_next_run",
    "recompute_next_run",
    "resume",
    "update_name",
    "update_pattern",
    "update_timezone",
]


def _validate_timezone(name: str) -> None:
    """Raise InvalidTimezoneError if `name` is not a recognised IANA zone.

    Central gate for every write that sets `schedules.timezone`. Attaches
    close-match suggestions so the handler can forward the message to the
    user verbatim.
    """
    if not is_valid_timezone(name):
        raise InvalidTimezoneError(name, suggestions=suggest_timezones(name))


# States in which next_planned_run_at carries a meaningful value. Any other
# state (paused, empty_paused) clears the column via the underlying query.
_ACTIVE_STATES: frozenset[str] = frozenset({"active"})


# ---------------------------------------------------------------------------
# Pure helper (no DB)
# ---------------------------------------------------------------------------


def next_planned_for(schedule: dict[str, Any], *, after: datetime) -> datetime | None:
    """Return the next planned fire time for `schedule`, or None.

    Returns None when the schedule is not active or has an invalid pattern;
    otherwise returns `calculate_next_run(after=after)`. Pure function, no DB.
    """
    state = str(schedule.get("state") or "")
    if state not in _ACTIVE_STATES:
        return None
    try:
        return calculate_next_run(schedule, after=after)
    except ValueError as e:
        logger.warning(
            "Cannot compute next run for schedule id=%s (invalid pattern): %s",
            schedule.get("id"),
            e,
        )
        return None


# ---------------------------------------------------------------------------
# Recompute (idempotent; caller-safe)
# ---------------------------------------------------------------------------


async def recompute_next_run(
    schedule_id: int, *, now: datetime | None = None
) -> datetime | None:
    """Recompute and persist `schedules.next_planned_run_at` for one schedule.

    Idempotent: safe to call after any state-changing op. Returns the new
    value (or None when cleared).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    schedule = await db.get_schedule(schedule_id)
    if schedule is None:
        return None

    next_at = next_planned_for(schedule, after=now)
    await persist_next_run(schedule_id, next_at)
    return next_at


async def persist_next_run(schedule_id: int, value: datetime | None) -> None:
    """Write a pre-computed next-run value to the schedule.

    Used by the engine's catch-up and FIFO defensive-backfill paths, which
    already have the `schedule` dict in hand and don't need the extra
    `get_schedule` round-trip that `recompute_next_run` performs.
    """
    await db.update_schedule_next_planned_run(schedule_id, value)


# ---------------------------------------------------------------------------
# Lifecycle commands (state transitions + content edits)
# ---------------------------------------------------------------------------


async def create(
    *,
    channel_db_id: int,
    name: str,
    pattern: dict[str, Any],
    timezone_name: str,
    state: str = "paused",
) -> dict[str, Any]:
    """Create a schedule. Defaults to paused so NPR stays NULL until the user
    explicitly resumes (which triggers the first NPR computation).

    Raises `InvalidTimezoneError` if `timezone_name` is not a recognised
    IANA name; the exception carries close-match suggestions.
    """
    _validate_timezone(timezone_name)
    return await db.create_schedule(
        channel_db_id=channel_db_id,
        name=name,
        pattern=pattern,
        timezone_name=timezone_name,
        state=state,
    )


async def update_pattern(
    schedule_id: int, pattern: dict[str, Any], *, user_id: int
) -> None:
    """Persist a new pattern and recompute NPR.

    Recomputing pins NPR to the first pattern slot strictly after now, so
    the next tick cannot fire a slot that the new pattern places in the
    past.
    """
    await db.update_schedule_pattern(schedule_id, pattern, user_id=user_id)
    await recompute_next_run(schedule_id)


async def update_timezone(
    schedule_id: int, *, timezone_name: str, user_id: int
) -> None:
    """Persist a new timezone and recompute NPR.

    Timezone changes can move daily/weekly slots; recompute pins NPR to the
    next slot under the new wall-clock target.

    Raises `InvalidTimezoneError` if `timezone_name` is not a recognised
    IANA name; the exception carries close-match suggestions.
    """
    _validate_timezone(timezone_name)
    await db.update_schedule_timezone(
        schedule_id, timezone_name=timezone_name, user_id=user_id
    )
    await recompute_next_run(schedule_id)


async def update_name(schedule_id: int, *, name: str, user_id: int) -> None:
    """Rename a schedule. Does not affect NPR."""
    await db.update_schedule_name(schedule_id, name=name, user_id=user_id)


async def pause(schedule_id: int, *, user_id: int) -> None:
    """Move to 'paused'. The underlying state-update SQL also clears NPR
    atomically, so no separate recompute call is needed."""
    await db.update_schedule_state(schedule_id, "paused", user_id=user_id)


async def resume(schedule_id: int, *, user_id: int) -> None:
    """Move to 'active' and recompute NPR.

    `db.resume_schedule` carries conditional logic for the legal source
    states (paused / empty_paused) and does not clear or set NPR itself;
    the recompute populates NPR for the now-active schedule.
    """
    await db.resume_schedule(schedule_id, user_id=user_id)
    await recompute_next_run(schedule_id)


async def mark_empty(schedule_id: int, *, user_id: int) -> None:
    """Move to 'empty_paused' (used by the engine when the queue runs dry).
    The state-update SQL also clears NPR atomically."""
    await db.update_schedule_state(schedule_id, "empty_paused", user_id=user_id)


async def delete(schedule_id: int, *, user_id: int) -> None:
    """Delete schedule and (cascading via FK) its queued posts, fingerprints
    pointing at those posts, etc. Owner-scoped at the SQL level."""
    await db.delete_schedule(schedule_id, user_id=user_id)
