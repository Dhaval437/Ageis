"""The `scopes` table: named scopes, remembered across restarts (`P3-10`).

This only remembers, like `settings.py`. What a folder may be, and whether a stored
one still may be, is `guardian/scope.py`'s to decide — every record read back goes
through `load_scope()` there before anything is allowed by it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from aegis_core.storage.db import StorageError, connect, default_db_path
from aegis_core.storage.usage import utc_timestamp

log = logging.getLogger(__name__)

_LIST: Final = "SELECT id, name, folders_json, apps_json FROM scopes ORDER BY name COLLATE NOCASE"
_GET: Final = "SELECT id, name, folders_json, apps_json FROM scopes WHERE id = ?"
_INSERT: Final = """
INSERT INTO scopes (name, folders_json, apps_json, created_at, updated_at)
VALUES (?, ?, ?, ?, ?)
"""
_UPDATE: Final = """
UPDATE scopes SET name = ?, folders_json = ?, apps_json = ?, updated_at = ? WHERE id = ?
"""
_DELETE: Final = "DELETE FROM scopes WHERE id = ?"


@dataclass(frozen=True, slots=True)
class ScopeRecord:
    """A row as stored. Not a scope yet: `guardian.scope.load_scope()` makes one."""

    id: int
    name: str
    folders: tuple[str, ...]
    apps: tuple[str, ...]


def _strings(raw: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("not a list")
    return tuple(item for item in value if isinstance(item, str))


class ScopeStore:
    """One connection, one lock, one table — the same shape as `SettingsStore`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, path: Path | None = None) -> Self:
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
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def list(self) -> tuple[ScopeRecord, ...]:
        """Every readable scope, by name. An unreadable row is skipped with a warning."""
        with self._lock:
            self._require_open()
            try:
                rows = self._conn.execute(_LIST).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read scopes: {error}") from error
        records = (self._record(row) for row in rows)
        return tuple(record for record in records if record is not None)

    def get(self, scope_id: int) -> ScopeRecord | None:
        with self._lock:
            self._require_open()
            try:
                row = self._conn.execute(_GET, (scope_id,)).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read scope {scope_id}: {error}") from error
        return None if row is None else self._record(row)

    def save(
        self, name: str, folders: tuple[str, ...], apps: tuple[str, ...], scope_id: int | None
    ) -> int:
        """Insert (no id) or replace (an id) a scope. Returns its id.

        A name another scope already has is a `StorageError` the user can act on.
        """
        now = utc_timestamp()
        encoded = (json.dumps(list(folders)), json.dumps(list(apps)))
        with self._lock:
            self._require_open()
            try:
                if scope_id is None:
                    cursor = self._conn.execute(_INSERT, (name, *encoded, now, now))
                    new_id = cursor.lastrowid
                    if new_id is None:
                        raise StorageError("the scope was not saved")
                    return new_id
                cursor = self._conn.execute(_UPDATE, (name, *encoded, now, scope_id))
            except sqlite3.IntegrityError as error:
                raise StorageError("Another scope already has that name.") from error
            except sqlite3.Error as error:
                raise StorageError(f"cannot save the scope: {error}") from error
        if cursor.rowcount == 0:
            raise StorageError("That scope no longer exists.")
        return scope_id

    def delete(self, scope_id: int) -> bool:
        with self._lock:
            self._require_open()
            try:
                cursor = self._conn.execute(_DELETE, (scope_id,))
            except sqlite3.Error as error:
                raise StorageError(f"cannot remove scope {scope_id}: {error}") from error
        return cursor.rowcount > 0

    @staticmethod
    def _record(row: tuple[object, ...]) -> ScopeRecord | None:
        scope_id, name, folders_json, apps_json = row
        try:
            return ScopeRecord(
                id=int(str(scope_id)),
                name=str(name),
                folders=_strings(str(folders_json)),
                apps=_strings(str(apps_json)),
            )
        except ValueError as error:
            # Folders are the user's paths: the row id is logged, never its contents.
            log.warning("scopes.unreadable", extra={"id": scope_id, "error": type(error).__name__})
            return None

    def _require_open(self) -> None:
        """Caller holds `_lock`."""
        if self._closed:
            raise StorageError("the scope store is closed")
