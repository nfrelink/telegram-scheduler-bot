"""Logging configuration for the bot.

Default mode is JSON: one structured record per line, suitable for ingestion
by ``jq``, ``journalctl -o json``, or any log aggregator. Set
``LOG_FORMAT=text`` to keep the previous human-readable output for local
development.

Tag operationally-interesting log calls with
``extra={"event": "post_sent", "schedule_id": ...}``; the JSON formatter
promotes any non-reserved key on the ``LogRecord`` to a top-level field, so
``jq 'select(.event=="post_sent")'`` works without re-parsing the message.

Bot tokens are scrubbed from both ``message`` and any string field in
``extra`` before emit; the redaction also covers text-mode output and the
formatted exception traceback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

# `LogRecord` attributes set by the logging module itself. Anything not in
# this set was attached via ``extra=`` and should appear at the top of the
# JSON record.
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

# Bot tokens leak via ``httpx`` error messages, request URLs in tracebacks,
# and anywhere a developer accidentally logs ``bot.token``. Match both the
# ``bot<digits>:<token>`` library prefix (used in PTB internals) and the bare
# ``<digits>:<token>`` Telegram format so neither shape survives to disk.
_BOT_PREFIX_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def _redact(text: str) -> str:
    text = _BOT_PREFIX_TOKEN_RE.sub("bot<redacted>", text)
    text = _TOKEN_RE.sub("<redacted>", text)
    return text


def _json_safe(value: Any) -> Any:
    """Coerce arbitrary ``extra`` values into JSON-serialisable shapes.

    Numbers, strings, bools, and ``None`` pass through. ``datetime`` becomes
    its ISO string. Lists / tuples / dicts recurse. Everything else is
    rendered with ``str()`` so a caller adding ``extra={"schedule": dict}``
    or ``extra={"path": Path("...")}`` doesn't silently drop the record.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    """Stdlib-only JSON log formatter.

    Output shape (one line; ``StreamHandler`` adds the trailing newline)::

        {
            "ts": "2026-04-20T12:34:56.789012+00:00",
            "level": "info",
            "logger": "scheduler.engine",
            "event": "post_sent",
            "message": "Posted message ...",
            "schedule_id": 17,
            "post_id": 42,
            "error": {
                "type": "BadRequest",
                "message": "Chat not found",
                "traceback": "Traceback ..."
            }
        }

    ``event`` and ``error`` only appear when the caller supplied them
    (``extra={"event": ...}`` and ``exc_info=True`` respectively).
    """

    def format(self, record: logging.LogRecord) -> str:
        message = _redact(record.getMessage())

        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
        }

        # Promote every non-reserved attribute on the record. ``event`` is
        # conventional; everything else (schedule_id, post_id, channel_id,
        # ...) appears as-is so jq can filter on it directly.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)

        payload["message"] = message

        if record.exc_info:
            exc_type, exc_value, _exc_tb = record.exc_info
            payload["error"] = {
                "type": exc_type.__name__ if exc_type is not None else "Exception",
                "message": _redact(str(exc_value) if exc_value is not None else ""),
                "traceback": _redact(self.formatException(record.exc_info)),
            }

        # ``default=str`` is the final safety net for anything ``_json_safe``
        # missed (custom objects buried inside a list, etc.). Re-redact the
        # final string so token-shaped substrings inside fallback ``str()``
        # output cannot escape.
        return _redact(json.dumps(payload, default=str, ensure_ascii=False))


class _RedactingTextFormatter(logging.Formatter):
    """Text formatter that scrubs Telegram bot tokens.

    Used when ``LOG_FORMAT=text``; preserves the previous human-readable
    output (modulo the class name) for local development.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def setup_logging() -> None:
    """Install the configured formatter on the root logger.

    Reads two env vars:

    * ``LOG_FORMAT``: ``json`` (default) or ``text``. Anything else falls
      back to ``json`` — a typo in the env shouldn't silently degrade
      observability.
    * ``LOG_LEVEL``: ``INFO`` (default), case-insensitive. Unknown values
      fall back to INFO.

    Always pins ``httpx`` and ``httpcore`` to WARNING. They log request URLs
    that contain the bot token; the formatter scrubs it, but turning the
    volume down at the source also cuts the per-request noise that drowns
    out the schedule events worth watching.
    """
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level_name, logging.INFO)

    log_format = os.getenv("LOG_FORMAT", "json").lower()
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "text":
        handler.setFormatter(
            _RedactingTextFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
