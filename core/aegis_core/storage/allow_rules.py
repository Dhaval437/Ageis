"""The `allow_rules` table (`P3-11`). This only remembers, like `settings.py`.

What a rule matches, and when it may relax a decision at all, is
`guardian/allow_rules.py` and `guardian/policy.py`.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from aegis_core.storage.db import StorageError, connect, default_db_path
from aegis_core.storage.usage import utc_timestamp

_LIST: Final = (
    "SELECT id, kind, tool, folder, params_digest, task_id, created_at"
    " FROM allow_rules ORDER BY id DESC"
)
_INSERT: Final = """
INSERT INTO allow_rules (kind, tool, folder, params_digest, task_id, created_at)
VALUES (?, ?, ?, ?, ?, ?)
"""
_DELETE: Final = "DELETE FROM allow_rules WHERE id = ?"


@dataclass(frozen=True, slots=True)
class StoredRule:
    id: int
    kind: str
    tool: str
    folder: str | None
    params_digest: str | None
    task_id: str | None
    created_at: str


class AllowRuleStore:
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

    def list(self) -> tuple[StoredRule, ...]:
        with self._lock:
            self._require_open()
            try:
                rows = self._conn.execute(_LIST).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read always-allow rules: {error}") from error
        return tuple(StoredRule(*row) for row in rows)

    def add(
        self,
        kind: str,
        tool: str,
        *,
        folder: str | None = None,
        params_digest: str | None = None,
        task_id: str | None = None,
    ) -> StoredRule:
        now = utc_timestamp()
        with self._lock:
            self._require_open()
            try:
                cursor = self._conn.execute(
                    _INSERT, (kind, tool, folder, params_digest, task_id, now)
                )
            except sqlite3.Error as error:
                raise StorageError(f"cannot save the always-allow rule: {error}") from error
        if cursor.lastrowid is None:
            raise StorageError("the always-allow rule was not saved")
        return StoredRule(cursor.lastrowid, kind, tool, folder, params_digest, task_id, now)

    def delete(self, rule_id: int) -> bool:
        with self._lock:
            self._require_open()
            try:
                cursor = self._conn.execute(_DELETE, (rule_id,))
            except sqlite3.Error as error:
                raise StorageError(f"cannot remove rule {rule_id}: {error}") from error
        return cursor.rowcount > 0

    def _require_open(self) -> None:
        """Caller holds `_lock`."""
        if self._closed:
            raise StorageError("the rule store is closed")
