"""Shared timezone utilities."""

from __future__ import annotations

import difflib
import os
from zoneinfo import ZoneInfo, available_timezones


class InvalidTimezoneError(ValueError):
    """Raised when a user-supplied timezone name fails validation.

    `str(exc)` is a user-friendly message that includes close-match
    suggestions when any exist, so callers can surface it directly to
    the user without crafting their own wording.
    """

    def __init__(self, name: str, *, suggestions: list[str] | None = None) -> None:
        self.name = name
        self.suggestions: list[str] = list(suggestions) if suggestions else []
        if self.suggestions:
            msg = (
                f"Unknown timezone: {name!r}. "
                f"Did you mean: {', '.join(self.suggestions)}?"
            )
        else:
            msg = (
                f"Unknown timezone: {name!r}. "
                "Use an IANA timezone name like Europe/Amsterdam or UTC."
            )
        super().__init__(msg)


def default_timezone_name() -> str:
    return os.getenv("DEFAULT_TIMEZONE", "UTC") or "UTC"


def is_valid_timezone(tz_name: str) -> bool:
    if tz_name.upper() in {"UTC", "ETC/UTC"}:
        return True
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


def suggest_timezones(name: str, *, limit: int = 3) -> list[str]:
    """Return up to `limit` IANA timezone names similar to `name`.

    Uses `difflib.get_close_matches` over the full `zoneinfo.available_timezones()`
    set. `limit` is bounded to a small number: we want to nudge the user,
    not overwhelm them. Returns [] when nothing is close enough, so the
    caller can fall back to a generic "unknown timezone" message.

    The search is case-sensitive because IANA names are canonically mixed
    case (`Europe/Amsterdam`), and difflib matches against the capitalised
    names give better signal-to-noise than a lowercase comparison would.
    """
    if not name or limit <= 0:
        return []
    candidates = sorted(available_timezones())
    return difflib.get_close_matches(name, candidates, n=limit, cutoff=0.6)
