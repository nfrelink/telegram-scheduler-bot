"""Tests for `src/utils/tz.py` — `is_valid_timezone`, `suggest_timezones`,
and the `InvalidTimezoneError` construction contract."""

from __future__ import annotations

from utils.tz import (
    InvalidTimezoneError,
    default_timezone_name,
    is_valid_timezone,
    suggest_timezones,
)


# ---------------------------------------------------------------------------
# is_valid_timezone
# ---------------------------------------------------------------------------

def test_is_valid_timezone_accepts_utc_case_insensitively() -> None:
    for tz in ("UTC", "utc", "Utc", "Etc/UTC", "etc/utc"):
        assert is_valid_timezone(tz)


def test_is_valid_timezone_accepts_canonical_iana_names() -> None:
    for tz in ("Europe/Amsterdam", "America/New_York", "Asia/Kolkata"):
        assert is_valid_timezone(tz)


def test_is_valid_timezone_rejects_nonsense_and_typos() -> None:
    for tz in ("Foo/Bar", "Europe/Amsterdamm", "", "America/NewYork"):
        assert not is_valid_timezone(tz)


# ---------------------------------------------------------------------------
# suggest_timezones
# ---------------------------------------------------------------------------

def test_suggest_timezones_finds_typo_correction() -> None:
    """Close typos of real names should produce the real name as a
    suggestion — the whole point of the helper."""
    suggestions = suggest_timezones("Europe/Amsterdamm")
    assert "Europe/Amsterdam" in suggestions


def test_suggest_timezones_returns_empty_for_nonsense() -> None:
    """No close matches -> empty list, so handlers can cleanly fall back
    to their generic 'unknown timezone' message."""
    assert suggest_timezones("xyz-garbage") == []


def test_suggest_timezones_respects_limit() -> None:
    """`limit` caps the output so a fuzzy query can't flood the user with
    dozens of possibilities."""
    all_suggestions = suggest_timezones("America/", limit=10)
    capped = suggest_timezones("America/", limit=2)
    assert len(capped) <= 2
    assert len(capped) <= len(all_suggestions)


def test_suggest_timezones_zero_or_empty_short_circuit() -> None:
    """Edge cases that don't merit a full scan of
    `zoneinfo.available_timezones()`."""
    assert suggest_timezones("", limit=5) == []
    assert suggest_timezones("Europe/Amsterdam", limit=0) == []


# ---------------------------------------------------------------------------
# InvalidTimezoneError
# ---------------------------------------------------------------------------

def test_invalid_timezone_error_message_includes_suggestions() -> None:
    err = InvalidTimezoneError(
        "Europe/Amsterdamm", suggestions=["Europe/Amsterdam", "Europe/Bucharest"]
    )
    msg = str(err)
    assert "Europe/Amsterdamm" in msg
    assert "Did you mean" in msg
    assert "Europe/Amsterdam" in msg


def test_invalid_timezone_error_message_without_suggestions() -> None:
    """No suggestions -> generic help hint, no dangling 'Did you mean:'."""
    err = InvalidTimezoneError("totally-not-a-tz")
    msg = str(err)
    assert "totally-not-a-tz" in msg
    assert "Did you mean" not in msg
    assert "IANA" in msg


def test_invalid_timezone_error_is_a_value_error() -> None:
    """Subclasses `ValueError` so existing `except ValueError` handlers
    still catch it. Callers who care about the type can still isinstance."""
    err = InvalidTimezoneError("x")
    assert isinstance(err, ValueError)


# ---------------------------------------------------------------------------
# default_timezone_name — env handling
# ---------------------------------------------------------------------------

def test_default_timezone_name_falls_back_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    assert default_timezone_name() == "UTC"


def test_default_timezone_name_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Europe/Amsterdam")
    assert default_timezone_name() == "Europe/Amsterdam"
