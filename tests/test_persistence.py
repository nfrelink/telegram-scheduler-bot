"""Tests for src/persistence.py — `build_persistence()` configuration,
round-trip of representative `user_data` payloads, atomic on-disk writes,
and graceful fallback when the on-disk pickle file is corrupt."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from persistence import (
    _AtomicPicklePersistence,
    _NoopPersistence,
    build_persistence,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_build_persistence_returns_configured_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No existing file: returns an atomic PicklePersistence with the
    expected flush interval and all four data slots enabled."""
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(tmp_path / "state.pickle"))

    persistence = build_persistence()
    assert isinstance(persistence, _AtomicPicklePersistence)

    assert persistence.update_interval == 15

    store = persistence.store_data
    assert store.user_data is True
    assert store.chat_data is True
    assert store.bot_data is True
    assert store.callback_data is True


def test_build_persistence_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-run safety: the parent directory is created if missing so the
    persistence file can be written on the first flush."""
    nested = tmp_path / "deeper" / "still" / "state.pickle"
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(nested))

    persistence = build_persistence()

    assert isinstance(persistence, _AtomicPicklePersistence)
    assert nested.parent.is_dir()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StagedItem:
    """Mirror of `bulk_upload._CollectedItem` shape: frozen dataclass with
    only primitive fields. The point of this fixture is to be a stand-in
    we can compare with `==` after a pickle round-trip without depending
    on production internals."""

    media_type: str
    file_id: str
    caption: str | None
    forward_from_chat_id: int | None
    raw_origin_is_forwarded: bool


@pytest.mark.asyncio
async def test_user_data_round_trips_representative_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload shaped like `bulk_upload`'s `user_data` (frozen dataclass
    items, list-of-dict caption entities, primitive scalars) must survive
    write → flush → reload."""
    pickle_path = tmp_path / "state.pickle"
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(pickle_path))

    payload: dict[str, object] = {
        "bulk_schedule_id": 42,
        "bulk_caption_mode": "shared",
        "bulk_single_caption": "Hello world",
        "bulk_single_caption_entities": [
            {"type": "bold", "offset": 0, "length": 5},
            {"type": "italic", "offset": 6, "length": 5},
        ],
        "bulk_dup_seq": 3,
        "bulk_dup_map": {1: "abc", 2: "def"},
        "bulk_in_confirming": False,
        "bulk_pending_splits": [
            _StagedItem("photo", "f1", "cap", None, False),
            _StagedItem("video", "f2", None, 1234, True),
        ],
    }

    writer = build_persistence()
    assert isinstance(writer, _AtomicPicklePersistence)
    await writer.update_user_data(user_id=7000, data=payload)
    await writer.flush()

    assert pickle_path.exists() and pickle_path.stat().st_size > 0

    reader = build_persistence()
    assert isinstance(reader, _AtomicPicklePersistence)
    restored = await reader.get_user_data()

    assert 7000 in restored
    assert restored[7000] == payload


# ---------------------------------------------------------------------------
# Atomic write semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_write_leaves_no_temp_file_after_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful flush should leave only the final pickle on disk; the
    `<name>.<pid>.tmp` sidecar must be renamed away, never accumulate."""
    pickle_path = tmp_path / "state.pickle"
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(pickle_path))

    writer = build_persistence()
    assert isinstance(writer, _AtomicPicklePersistence)
    await writer.update_user_data(user_id=1, data={"k": "v"})
    await writer.flush()

    contents = sorted(p.name for p in tmp_path.iterdir())
    assert contents == ["state.pickle"], f"unexpected files: {contents}"


@pytest.mark.asyncio
async def test_atomic_write_preserves_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the new write fails (e.g. process killed mid-pickle), the previous
    file contents must remain intact and the temp file must not linger.

    This is the whole point of the atomic write: the upstream `_dump_singlefile`
    truncates `state.pickle` before writing, so a crash there leaves the file
    half-written; we instead write to a sibling temp and only `os.replace` it
    in once the write has fully succeeded.
    """
    pickle_path = tmp_path / "state.pickle"
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(pickle_path))

    writer = build_persistence()
    assert isinstance(writer, _AtomicPicklePersistence)
    await writer.update_user_data(user_id=1, data={"first": True})
    await writer.flush()
    good_bytes = pickle_path.read_bytes()
    assert good_bytes, "first flush should have written a non-empty file"

    # Inject failure between the temp-file write and the rename. `os.fsync`
    # is the natural seam because (a) it runs after `pickle.dump` so the
    # temp file is fully populated when we explode and (b) `pickle.Pickler`
    # is a C type whose `dump` attribute can't be monkey-patched.
    # Note: with `on_flush=False` (PTB default), `update_user_data` itself
    # calls `_dump_singlefile`, so the patch has to wrap the write call,
    # not a separate `flush()`.
    with (
        patch("persistence.os.fsync", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await writer.update_user_data(user_id=1, data={"second": True})

    assert pickle_path.read_bytes() == good_bytes, (
        "previous pickle contents must survive a failed write"
    )
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temp files leaked after failed write: {leftovers}"


@pytest.mark.asyncio
async def test_atomic_write_temp_file_uses_pid_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the temp-file naming convention so two processes accidentally
    sharing a pickle path collide on the rename rather than on the temp
    file write itself (which would then race the truncate)."""
    pickle_path = tmp_path / "state.pickle"
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(pickle_path))

    writer = build_persistence()
    assert isinstance(writer, _AtomicPicklePersistence)
    await writer.update_user_data(user_id=1, data={"k": "v"})

    seen: list[Path] = []
    real_replace = os.replace

    def _capture_replace(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    with patch("persistence.os.replace", side_effect=_capture_replace):
        await writer.flush()

    assert len(seen) == 1
    assert seen[0].name == f"state.pickle.{os.getpid()}.tmp"


# ---------------------------------------------------------------------------
# Corrupt-file fallback
# ---------------------------------------------------------------------------


def test_build_persistence_returns_noop_when_pickle_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A garbled pickle file must not prevent the bot from starting. We
    return a no-op `BasePersistence` (not `None`) so that
    `Application.add_handler` accepts our `persistent=True` ConversationHandlers
    without raising."""
    bad_file = tmp_path / "state.pickle"
    bad_file.write_bytes(b"this is not a pickle stream")
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(bad_file))

    with caplog.at_level(logging.ERROR, logger="persistence"):
        persistence = build_persistence()

    assert isinstance(persistence, _NoopPersistence)
    assert any("unreadable" in rec.message.lower() for rec in caplog.records)


def test_build_persistence_returns_noop_when_pickle_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truncated pickle (EOF mid-stream) is also handled gracefully."""
    bad_file = tmp_path / "state.pickle"
    bad_file.write_bytes(b"\x80\x04")
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(bad_file))

    persistence = build_persistence()

    assert isinstance(persistence, _NoopPersistence)


@pytest.mark.asyncio
async def test_noop_persistence_satisfies_interface() -> None:
    """`Application` calls into the persistence at predictable points
    (boot, after every handler, on shutdown). The no-op fallback must
    satisfy each of those calls without raising and without claiming to
    have any data."""
    noop = _NoopPersistence()

    assert noop.store_data.user_data is False
    assert noop.store_data.chat_data is False
    assert noop.store_data.bot_data is False
    assert noop.store_data.callback_data is False

    assert await noop.get_user_data() == {}
    assert await noop.get_chat_data() == {}
    assert await noop.get_bot_data() == {}
    assert await noop.get_callback_data() is None
    assert await noop.get_conversations("anything") == {}

    await noop.update_user_data(1, {"x": 1})
    await noop.update_chat_data(1, {"x": 1})
    await noop.update_bot_data({"x": 1})
    await noop.update_callback_data(([], {}))
    await noop.update_conversation("name", (1,), 0)
    await noop.refresh_user_data(1, {})
    await noop.refresh_chat_data(1, {})
    await noop.refresh_bot_data({})
    await noop.drop_user_data(1)
    await noop.drop_chat_data(1)
    await noop.flush()


# ---------------------------------------------------------------------------
# End-to-end startup
# ---------------------------------------------------------------------------


def test_create_application_starts_when_pickle_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the original Phase 2.1 bug: returning `None` from
    `build_persistence` made the bot crash at handler-registration time
    because `Application.add_handler` rejects `persistent=True` handlers
    when the application has no persistence. The no-op fallback must let
    the full `create_application()` path succeed end-to-end so a corrupt
    pickle is recoverable without manual intervention.
    """
    bad_file = tmp_path / "state.pickle"
    bad_file.write_bytes(b"clearly not a pickle")
    monkeypatch.setenv("BOT_PERSISTENCE_PATH", str(bad_file))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:test_token_for_create_application")

    from bot import create_application  # noqa: PLC0415 — import after env monkeypatch

    application = create_application()

    assert isinstance(application.persistence, _NoopPersistence)
    # The persistent ConversationHandlers must have been accepted; if the
    # ValueError still fired, create_application would have raised before
    # adding most of these.
    total_handlers = sum(len(group) for group in application.handlers.values())
    assert total_handlers >= 15, f"unexpectedly few handlers registered: {total_handlers}"
