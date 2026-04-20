"""Architectural guard: handlers and the scheduler engine must not call the
mutating DB primitives that the services layer owns.

Three services (`scheduling`, `posting`, `dedup`) own writes that span
multiple tables, enforce cross-table invariants, or wrap atomic
transactions. Direct `db.<func>` / `queries.<func>` calls to those names
from outside the services layer (and outside the database layer itself)
silently bypass those invariants.

Scope:
    - Files under `src/handlers/**` and `src/scheduler/**`.
    - Forbidden symbols are listed in `_FORBIDDEN`.
    - Read-only sibling queries (`get_*`) are intentionally still allowed
      as direct `db.*` calls; they hold no invariants.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Source roots
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCAN_ROOTS: tuple[Path, ...] = (
    _PROJECT_ROOT / "src" / "handlers",
    _PROJECT_ROOT / "src" / "scheduler",
)


# ---------------------------------------------------------------------------
# Symbols owned by the services layer
# ---------------------------------------------------------------------------

# scheduling.*
_FORBIDDEN_SCHEDULING: frozenset[str] = frozenset({
    "create_schedule",
    "update_schedule_state",
    "update_schedule_pattern",
    "update_schedule_timezone",
    "update_schedule_name",
    "update_schedule_next_planned_run",
    "resume_schedule",
    "delete_schedule",
})

# posting.*
_FORBIDDEN_POSTING: frozenset[str] = frozenset({
    "add_queued_posts_bulk",
    "delete_queued_post",
    "update_post_retry",
    "bulk_update_posts_scheduled_for",
    "set_post_pinned_at",
    "clear_post_pinned_at",
    # The four send-completion orchestrator names. They live on
    # `services.posting`; an attempt to import them via `db.complete_post_send`
    # etc. would already fail at import time. Listed here so reintroducing them
    # on `database.queries` would also flag.
    "complete_post_send",
    "complete_post_retry",
    "complete_post_failure_pause",
    "cancel_queued_post",
})

# dedup.*
_FORBIDDEN_DEDUP: frozenset[str] = frozenset({
    "get_channel_duplicate_detection",
    "set_channel_duplicate_detection",
    "get_user_duplicate_alerts",
    "set_user_duplicate_alerts",
    "find_fingerprint_by_file_unique_id",
    "get_channel_dhashes",
    "get_fingerprint",
    "add_fingerprints_bulk",
    "mark_fingerprint_posted",
    "delete_unposted_fingerprints",
})

_FORBIDDEN: frozenset[str] = (
    _FORBIDDEN_SCHEDULING | _FORBIDDEN_POSTING | _FORBIDDEN_DEDUP
)


# Match `db.<sym>` and `queries.<sym>`; the scan ignores text inside string
# literals and comments only minimally (Python source is regular enough that
# false positives here are fine — they can be silenced with a service-level
# rewrite, which is exactly the desired outcome).
_CALL_PATTERN = re.compile(
    r"\b(?:db|queries)\.(" + "|".join(re.escape(s) for s in sorted(_FORBIDDEN)) + r")\b"
)


def _iter_python_files(root: Path):
    for p in root.rglob("*.py"):
        # Skip __pycache__ etc.
        if "__pycache__" in p.parts:
            continue
        yield p


@pytest.mark.parametrize("root", _SCAN_ROOTS, ids=lambda p: p.name)
def test_no_direct_db_calls_to_service_owned_symbols(root: Path) -> None:
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_python_files(root):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _CALL_PATTERN.search(line):
                offenders.append((str(path.relative_to(_PROJECT_ROOT)), lineno, line.strip()))

    if offenders:
        rendered = "\n".join(f"  {p}:{ln}: {src}" for p, ln, src in offenders)
        pytest.fail(
            "Found direct db.*/queries.* calls into service-owned symbols.\n"
            "Use the matching services.<scheduling|posting|dedup>.* function instead.\n\n"
            f"Offenders:\n{rendered}"
        )
