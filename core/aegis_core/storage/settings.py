"""The settings table — one JSON document per key, durable across restarts.

`ARCHITECTURE.md § 7` gives `settings` two columns and nothing else: a key and a JSON
value. That is deliberate. Settings are read by screens, not by queries, and a schema
per section would be a migration every time a checkbox is added.

What this module adds on top of "write JSON to a row" is the two things a key-value
store gets wrong if nobody decides them:

- **A value is bounded.** `REVIEW.md § 2` forbids an unbounded anything, and the
  renderer is a hostile caller (`ARCHITECTURE.md § 9.3`) — it displays text the agent
  scraped off the user's screen and could hand a megabyte of it to `PUT /settings`.
- **A value that cannot be read is not a crash.** A row written by a newer build, or
  edited by hand, is answered as *absent* with a logged reason, so the app starts with
  defaults rather than failing at the screen that would let the user fix it.

Who decides what a key *means* is `models/service.py` and the screens above it. This
only remembers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from pydantic import JsonValue

from aegis_core.storage.db import StorageError, connect, default_db_path

log = logging.getLogger(__name__)

#: The longest a serialised value may be. The model settings — three role chains, four
#: ceilings and two URLs — are a couple of kilobytes at most, so this is generous by an
#: order of magnitude and still small enough that a runaway writer fails loudly.
MAX_VALUE_BYTES: Final = 64 * 1024

#: A key is an identifier this build wrote, never user input; the cap is a guard against
#: a caller that has confused a key with a value.
MAX_KEY_LEN: Final = 64

_SELECT: Final = "SELECT value_json FROM settings WHERE key = ?"
_UPSERT: Final = """
INSERT INTO settings (key, value_json) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json
"""
_DELETE: Final = "DELETE FROM settings WHERE key = ?"


class SettingsStore:
    """One connection, one lock, one table. Read a document, write a document.

    Opened `cross_thread` and serialised under a lock for the same reason
    `storage/usage.py` is: the callers are not all on the event loop, and one connection
    under a lock is cheaper than a pool for a table written when a user clicks Save.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, path: Path | None = None) -> Self:
        """Open the store on `aegis.db`.

        The schema is not migrated here: `storage.db.bootstrap()` has already run at
        startup, before the core announced itself (`P0-10`).
        """
        return cls(connect(default_db_path() if path is None else path, cross_thread=True))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def get(self, key: str) -> JsonValue | None:
        """The saved document, or `None` if there is none — or none this build can read.

        Unreadable JSON is `None` with a warning rather than an exception: the row is
        already written, and the only screen that could replace it is the one that would
        fail to open.
        """
        row = self._read(key)
        if row is None:
            return None
        try:
            value: JsonValue = json.loads(row)
        except ValueError as error:
            # The value is settings, not a key — but it is still not quoted into a log
            # line, because a settings document can hold an address the user typed.
            log.warning("settings.unreadable", extra={"key": key, "error": type(error).__name__})
            return None
        return value

    def put(self, key: str, value: JsonValue) -> None:
        """Replace the document under `key`. Raises `StorageError` if it cannot."""
        self._check_key(key)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size > MAX_VALUE_BYTES:
            raise StorageError(f"that setting is {size} bytes; the limit is {MAX_VALUE_BYTES}.")
        with self._lock:
            self._require_open()
            try:
                self._conn.execute(_UPSERT, (key, encoded))
            except sqlite3.Error as error:
                raise StorageError(f"cannot save setting {key!r}: {error}") from error
        log.info("settings.saved", extra={"key": key, "bytes": size})

    def delete(self, key: str) -> bool:
        """Remove the document under `key`. Returns whether there was one."""
        self._check_key(key)
        with self._lock:
            self._require_open()
            try:
                cursor = self._conn.execute(_DELETE, (key,))
            except sqlite3.Error as error:
                raise StorageError(f"cannot remove setting {key!r}: {error}") from error
        return cursor.rowcount > 0

    def _read(self, key: str) -> str | None:
        self._check_key(key)
        with self._lock:
            self._require_open()
            try:
                row = self._conn.execute(_SELECT, (key,)).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read setting {key!r}: {error}") from error
        return None if row is None else str(row[0])

    def _check_key(self, key: str) -> None:
        if not key or len(key) > MAX_KEY_LEN:
            raise StorageError(f"a settings key must be 1 to {MAX_KEY_LEN} characters")

    def _require_open(self) -> None:
        """Caller holds `_lock`."""
        if self._closed:
            raise StorageError("the settings store is closed")
