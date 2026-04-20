"""Domain services — business operations that own multi-step writes and
cross-table invariants.

Layering:

    handlers / engine
        ↓ calls
    services        ← here
        ↓ calls
    database.queries (CRUD primitives + _in_tx helpers + connection pool)
        ↓
    SQLite

Rules of the layer:
    - Services own writes that involve more than one table or one invariant.
      Anything single-row read-only stays a direct `db.queries` call from the
      caller.
    - Services may call other services (posting → scheduling for next-run
      recomputation), but never the other way around.
    - Services use `database.queries.transaction()` for atomicity, composed
      from `_xxx_in_tx` helpers in queries.
    - No Telegram imports in services. They are unit-testable against the
      DB alone. The single approved exception is `notifications`, a
      side-effect adapter whose entire purpose is to talk to the bot;
      `notifications` calls Telegram but never the database.
"""

from . import dedup, notifications, posting, scheduling

__all__ = ["dedup", "notifications", "posting", "scheduling"]
