"""Conversation and context persistence for the Telegram bot.

Wraps PTB's `PicklePersistence` so that conversation state and
`context.user_data` survive bot restarts. Without this, killing the bot
mid-flow (during `/bulk`, `/schedules edit`, `/channels add`, etc.) drops
the user back to the entry point and loses any partially-collected media
or wizard input.

The configuration here:
    - persists every persistence slot (`user_data`, `chat_data`,
      `bot_data`, `callback_data`); the codebase only uses `user_data`
      today, but the others come along for free and avoid surprise gaps if
      a future handler starts using them;
    - flushes batched writes every 15 seconds (default is 60), trading a
      negligible amount of disk I/O for a smaller crash-loss window;
    - writes the on-disk pickle atomically via temp-file + `os.replace`,
      so a crash mid-write cannot leave a half-written file that would
      then refuse to load on next boot;
    - falls back to a no-op `BasePersistence` (with a loud log) if the
      existing on-disk pickle is corrupt at startup. The fallback must be
      a real `BasePersistence` instance, not `None`: `Application.add_handler`
      raises `ValueError` when adding a `persistent=True` ConversationHandler
      to an application without persistence, which would prevent the bot
      from booting at all.
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Any, Optional

from telegram._bot import Bot
from telegram.ext import BasePersistence, PersistenceInput, PicklePersistence
from telegram.ext._utils.types import CDCData, ConversationDict, ConversationKey

logger = logging.getLogger(__name__)

_DEFAULT_PERSISTENCE_PATH = "/app/data/conversation_state.pickle"
_FLUSH_INTERVAL_SECONDS = 15


def _persistence_path() -> Path:
    """Resolve the on-disk pickle path. `BOT_PERSISTENCE_PATH` overrides for
    local dev / tests; production uses the mounted `/app/data` volume."""
    return Path(os.getenv("BOT_PERSISTENCE_PATH", _DEFAULT_PERSISTENCE_PATH))


# ---------------------------------------------------------------------------
# Validating unpickler used to probe the file on startup
# ---------------------------------------------------------------------------


class _ValidatingUnpickler(pickle.Unpickler):
    """Unpickler that tolerates PicklePersistence's `persistent_id` markers.

    PTB's `PicklePersistence` swaps `telegram.Bot` instances for placeholder
    persistent ids on save (so that a token rotation doesn't break loaded
    data). Validating the file here therefore needs a `persistent_load`
    counterpart, otherwise a structurally healthy file is reported as
    corrupt. We don't care what the bot becomes during validation — we're
    only asking "does this file parse end-to-end" — so the sentinel is fine.
    """

    def persistent_load(self, pid: object) -> object:  # noqa: ARG002 - signature required
        return None


def _is_pickle_loadable(path: Path) -> bool:
    """Return True iff `path` is missing or contains a deserialisable pickle."""
    if not path.exists():
        return True
    try:
        with path.open("rb") as f:
            _ValidatingUnpickler(f).load()
    except (pickle.UnpicklingError, EOFError, AttributeError, ImportError) as e:
        logger.error(
            "Conversation persistence file is unreadable (%s: %s); "
            "starting with a no-op persistence instead. The bad file is at "
            "%s and can be deleted manually to clear the warning on next start.",
            type(e).__name__,
            e,
            path,
        )
        return False
    except Exception:
        logger.exception(
            "Unexpected failure while validating persistence file at %s; "
            "starting with a no-op persistence instead.",
            path,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Atomic single-file PicklePersistence
# ---------------------------------------------------------------------------


class _AtomicPicklePersistence(PicklePersistence):
    """`PicklePersistence` that writes the single-file pickle atomically.

    The upstream implementation does `with self.filepath.open("wb"): pickle.dump(...)`,
    which truncates the destination file before writing the new contents.
    A crash (OOM kill, container restart, host power loss) between the truncate
    and the final byte leaves a structurally invalid file on disk, which then
    refuses to load on the next boot.

    This subclass writes to a sibling temp file in the same directory and then
    uses `os.replace` to swap it into place. `os.replace` is atomic on both
    POSIX and Windows, so on-disk we always see either the previous full
    contents or the new full contents — never a partial write.

    We override `_dump_singlefile` only because we configure `single_file=True`.
    If a future change switches to multi-file mode, `_dump_file` would also
    need an atomic variant.
    """

    __slots__ = ()

    def _dump_singlefile(self) -> None:
        data = {
            "conversations": self.conversations,
            "user_data": self.user_data,
            "chat_data": self.chat_data,
            "bot_data": self.bot_data,
            "callback_data": self.callback_data,
        }
        # Write to a sibling temp file in the same directory so `os.replace`
        # stays on the same filesystem (cross-fs renames are not atomic and
        # would silently fall back to copy+delete on some platforms).
        # Including the pid in the suffix makes concurrent writes from
        # different processes (e.g. pytest workers, accidental double-start)
        # collide on rename rather than on the temp file itself.
        from telegram.ext._picklepersistence import _BotPickler  # noqa: PLC0415

        tmp_path = self.filepath.with_suffix(self.filepath.suffix + f".{os.getpid()}.tmp")
        try:
            with tmp_path.open("wb") as file:
                _BotPickler(self.bot, file, protocol=pickle.HIGHEST_PROTOCOL).dump(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.filepath)
        except BaseException:
            # If anything goes wrong before the rename, the temp file is
            # garbage; remove it so it can't accumulate or be mistaken for
            # the real pickle on a future inspection.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Failed to remove temp persistence file %s after a write error",
                    tmp_path,
                )
            raise


# ---------------------------------------------------------------------------
# No-op fallback persistence
# ---------------------------------------------------------------------------


class _NoopPersistence(BasePersistence):
    """`BasePersistence` that reads as empty and silently swallows writes.

    Used as the fallback when the on-disk pickle file is unreadable. The
    application is then free to register `persistent=True` ConversationHandlers
    without `add_handler` raising — the handlers will simply not survive a
    restart, which is the same behaviour as if persistence were disabled
    entirely from the start.

    All `store_data` flags are False so the application doesn't bother
    routing data to us in the first place. This also avoids `set_bot`'s
    `callback_data → ExtBot` type check, which is irrelevant here since
    we don't actually store anything.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(
            store_data=PersistenceInput(
                bot_data=False,
                chat_data=False,
                user_data=False,
                callback_data=False,
            ),
            update_interval=_FLUSH_INTERVAL_SECONDS,
        )

    def set_bot(self, bot: Bot) -> None:  # noqa: D401
        # Skip BasePersistence.set_bot's ExtBot check: we never store
        # callback_data so the check is moot, and we never need a bot
        # reference because we never serialise anything.
        self.bot = bot

    async def get_user_data(self) -> dict[int, Any]:
        return {}

    async def get_chat_data(self) -> dict[int, Any]:
        return {}

    async def get_bot_data(self) -> dict[str, Any]:
        return {}

    async def get_callback_data(self) -> Optional[CDCData]:
        return None

    async def get_conversations(self, name: str) -> ConversationDict:  # noqa: ARG002
        return {}

    async def update_conversation(
        self, name: str, key: ConversationKey, new_state: Optional[object]  # noqa: ARG002
    ) -> None:
        return None

    async def update_user_data(self, user_id: int, data: Any) -> None:  # noqa: ARG002
        return None

    async def update_chat_data(self, chat_id: int, data: Any) -> None:  # noqa: ARG002
        return None

    async def update_bot_data(self, data: Any) -> None:  # noqa: ARG002
        return None

    async def update_callback_data(self, data: CDCData) -> None:  # noqa: ARG002
        return None

    async def drop_chat_data(self, chat_id: int) -> None:  # noqa: ARG002
        return None

    async def drop_user_data(self, user_id: int) -> None:  # noqa: ARG002
        return None

    async def refresh_user_data(self, user_id: int, user_data: Any) -> None:  # noqa: ARG002
        return None

    async def refresh_chat_data(self, chat_id: int, chat_data: Any) -> None:  # noqa: ARG002
        return None

    async def refresh_bot_data(self, bot_data: Any) -> None:  # noqa: ARG002
        return None

    async def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_persistence() -> BasePersistence:
    """Construct the persistence instance to hand to `ApplicationBuilder`.

    Always returns a `BasePersistence`: a real atomic-pickle persistence on
    the happy path, a no-op fallback if the on-disk file is unreadable or
    the directory cannot be created. Returning `None` is not an option:
    `Application.add_handler` refuses `persistent=True` ConversationHandlers
    when the application has no persistence, which would prevent boot.
    """
    path = _persistence_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception(
            "Could not create persistence directory %s; starting with a "
            "no-op persistence instead.",
            path.parent,
        )
        return _NoopPersistence()

    if not _is_pickle_loadable(path):
        return _NoopPersistence()

    return _AtomicPicklePersistence(
        filepath=path,
        store_data=PersistenceInput(
            user_data=True,
            chat_data=True,
            bot_data=True,
            callback_data=True,
        ),
        update_interval=_FLUSH_INTERVAL_SECONDS,
    )
