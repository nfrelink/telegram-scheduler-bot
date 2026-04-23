"""Tests for src/logging_setup.py."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from logging_setup import JsonFormatter, setup_logging


def _format(record: logging.LogRecord) -> dict:
    """Format `record` with a JsonFormatter and return the parsed dict."""
    return json.loads(JsonFormatter().format(record))


def _make_record(
    *,
    level: int = logging.INFO,
    msg: str = "hello %s",
    args: tuple = ("world",),
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


# ---------------------------------------------------------------------------
# JsonFormatter — top-level shape
# ---------------------------------------------------------------------------


def test_json_format_required_fields_present() -> None:
    record = _make_record()
    payload = _format(record)
    assert payload["level"] == "info"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello world"
    # ts must be a parseable ISO 8601 timestamp in UTC
    ts = datetime.fromisoformat(payload["ts"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timezone.utc.utcoffset(ts)


def test_extra_keys_promoted_to_top_level() -> None:
    record = _make_record(
        extra={"event": "post_sent", "schedule_id": 17, "post_id": 42}
    )
    payload = _format(record)
    assert payload["event"] == "post_sent"
    assert payload["schedule_id"] == 17
    assert payload["post_id"] == 42


def test_reserved_record_attrs_not_emitted() -> None:
    record = _make_record(extra={"event": "x"})
    payload = _format(record)
    # These are real LogRecord attributes — they must never bleed into output.
    for forbidden in (
        "args",
        "msg",
        "levelno",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
    ):
        assert forbidden not in payload, f"{forbidden!r} leaked into JSON output"


# ---------------------------------------------------------------------------
# JsonFormatter — value coercion
# ---------------------------------------------------------------------------


def test_extra_datetime_serialised_as_iso() -> None:
    when = datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)
    record = _make_record(extra={"next_planned_run_at": when})
    payload = _format(record)
    assert payload["next_planned_run_at"] == "2026-04-20T12:34:56+00:00"


def test_extra_path_falls_back_to_str() -> None:
    record = _make_record(extra={"path": Path("/var/data/foo.pickle")})
    payload = _format(record)
    assert payload["path"] == "/var/data/foo.pickle"


def test_extra_nested_dict_recurses() -> None:
    record = _make_record(extra={"meta": {"schedule_id": 3, "labels": ["a", "b"]}})
    payload = _format(record)
    assert payload["meta"] == {"schedule_id": 3, "labels": ["a", "b"]}


# ---------------------------------------------------------------------------
# JsonFormatter — exc_info handling
# ---------------------------------------------------------------------------


def test_exc_info_produces_structured_error_block() -> None:
    try:
        raise ValueError("bad input")
    except ValueError:
        import sys

        record = _make_record(level=logging.ERROR, exc_info=sys.exc_info())
    payload = _format(record)
    assert payload["error"]["type"] == "ValueError"
    assert payload["error"]["message"] == "bad input"
    assert "Traceback" in payload["error"]["traceback"]


def test_no_error_block_without_exc_info() -> None:
    record = _make_record()
    payload = _format(record)
    assert "error" not in payload


# ---------------------------------------------------------------------------
# JsonFormatter — token redaction
# ---------------------------------------------------------------------------


def test_token_redacted_in_message() -> None:
    record = _make_record(
        msg="failed call: bot1234567890:AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPP", args=()
    )
    payload = _format(record)
    assert "AABBCCDD" not in payload["message"]
    assert "bot<redacted>" in payload["message"]


def test_bare_token_in_extra_is_redacted_via_final_pass() -> None:
    # `extra` strings flow through `_json_safe` unchanged but the final
    # `_redact(json.dumps(...))` pass scrubs token-shaped substrings anywhere
    # in the rendered payload. Use the bare `<digits>:<token>` form so the
    # `bot` prefix (which would also be matched directly by the bot-prefix
    # regex) is not what's doing the work.
    record = _make_record(
        msg="see context",
        args=(),
        extra={"context": "1234567890:AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPP"},
    )
    payload_text = JsonFormatter().format(record)
    assert "AABBCCDD" not in payload_text
    assert "<redacted>" in payload_text


def test_token_redacted_in_traceback() -> None:
    try:
        raise RuntimeError(
            "request failed: https://api.telegram.org/bot1234567890:AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPP/sendMessage"
        )
    except RuntimeError:
        import sys

        record = _make_record(level=logging.ERROR, exc_info=sys.exc_info())
    payload = _format(record)
    assert "AABBCCDD" not in payload["error"]["message"]
    assert "AABBCCDD" not in payload["error"]["traceback"]


# ---------------------------------------------------------------------------
# setup_logging — env handling
# ---------------------------------------------------------------------------


def _capture_stream() -> tuple[io.StringIO, logging.Handler]:
    """Return a stream + StreamHandler ready to be installed by setup_logging.

    The trick: we run setup_logging, then swap the StreamHandler's stream
    for our own buffer. That keeps the test honest about the formatter
    install path while letting us inspect the bytes that would have hit
    stdout.
    """
    buf = io.StringIO()
    root = logging.getLogger()
    handler = root.handlers[-1]
    handler.stream = buf  # type: ignore[attr-defined]
    return buf, handler


@pytest.fixture
def _restore_root_logging():
    """setup_logging mutates the root logger; snapshot and restore it."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers.clear()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_setup_logging_default_emits_json(
    monkeypatch: pytest.MonkeyPatch, _restore_root_logging
) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    setup_logging()
    buf, _ = _capture_stream()

    logging.getLogger("svc").info("hi", extra={"event": "test_event"})
    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload["event"] == "test_event"
    assert payload["message"] == "hi"


def test_setup_logging_text_mode(
    monkeypatch: pytest.MonkeyPatch, _restore_root_logging
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "text")
    setup_logging()
    buf, _ = _capture_stream()

    logging.getLogger("svc").info("hi there")
    text = buf.getvalue()
    # Text mode includes the level name verbatim and never JSON-quotes the
    # message; if either of those is wrong we've fallen back to JSON.
    assert "INFO" in text
    assert "hi there" in text
    assert not text.lstrip().startswith("{")


def test_setup_logging_unknown_format_falls_back_to_json(
    monkeypatch: pytest.MonkeyPatch, _restore_root_logging
) -> None:
    # A typo in LOG_FORMAT shouldn't silently degrade to plain text.
    monkeypatch.setenv("LOG_FORMAT", "yaml")
    setup_logging()
    buf, _ = _capture_stream()

    logging.getLogger("svc").info("hi")
    line = buf.getvalue().strip()
    json.loads(line)


def test_setup_logging_honours_log_level(
    monkeypatch: pytest.MonkeyPatch, _restore_root_logging
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    setup_logging()
    buf, _ = _capture_stream()

    logging.getLogger("svc").info("dropped")
    logging.getLogger("svc").warning("kept")

    output = buf.getvalue()
    assert "dropped" not in output
    assert "kept" in output


def test_setup_logging_silences_httpx(
    monkeypatch: pytest.MonkeyPatch, _restore_root_logging
) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
