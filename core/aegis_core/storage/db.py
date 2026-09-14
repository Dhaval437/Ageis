"""Opening `aegis.db` and bringing its schema up to date.

Every connection is opened through `connect()`, so every connection gets the same
guarantees:

- **WAL mode**, verified rather than assumed — readers never block the writer.
- **`synchronous = FULL`.** Invariant 11 is "journal before you act": a journal row
  that a power cut can take back after the action ran is not a write-ahead record.
- **Foreign keys on.** SQLite leaves them off per connection unless asked.
- **A busy timeout**, so a lock held elsewhere is an error after a bound wait, not
  a hang.

The schema version is `PRAGMA user_version`. `migrate()` applies each pending
migration and its version bump in one transaction, so a failure leaves the
database exactly at the previous version, and a database from a newer Aegis is
refused rather than written to by code that does not know its shape.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from aegis_core.storage.migrations import MIGRATIONS, Migration

log = logging.getLogger(__name__)

DB_FILE_NAME: Final = "aegis.db"

#: How long a statement waits on a lock held by another connection.
BUSY_TIMEOUT_S: Final = 5.0

#: `STRICT` tables need 3.37 and the built-in `json_valid` needs 3.38.
MIN_SQLITE_VERSION: Final = (3, 38, 0)


class StorageError(Exception):
    """The database could not be opened or brought up to date."""


def default_data_dir() -> Path:
    """`%LOCALAPPDATA%\\Aegis`, falling back to `~/.aegis` off Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Aegis"
    return Path.home() / ".aegis"


def default_db_path() -> Path:
    return default_data_dir() / DB_FILE_NAME


def connect(path: Path) -> sqlite3.Connection:
    """Open `path` (creating it and its folder) with the pragmas above applied.

    The connection is in autocommit mode (`isolation_level=None`): callers open
    transactions explicitly, so none is ever left open by accident.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise StorageError(f"SQLite {sqlite3.sqlite_version} is too old; 3.38 or newer is required")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    except (OSError, sqlite3.Error) as error:
        raise StorageError(f"cannot open the database: {error}") from error
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StorageError(f"the database refused WAL mode (journal_mode={mode})")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.Error as error:
        conn.close()
        raise StorageError(f"cannot configure the database: {error}") from error
    except StorageError:
        conn.close()
        raise
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Apply every migration newer than the database. Returns the final version."""
    _check_sequence(migrations)
    latest = migrations[-1].version if migrations else 0
    current = schema_version(conn)
    if current > latest:
        raise StorageError(
            f"the database is at schema version {current}, newer than this build "
            f"understands ({latest}); refusing to open it"
        )

    for migration in migrations:
        if migration.version <= current:
            continue
        # One script, one transaction: the DDL and the version bump commit together.
        # `executescript` would silently commit a transaction opened before it, which
        # is why BEGIN lives inside the script.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            f"PRAGMA user_version = {int(migration.version)};\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if schema_version(conn) >= migration.version:
                # Another process applied it between our read and our write.
                current = schema_version(conn)
                continue
            raise StorageError(
                f"migration {migration.version} ({migration.name}) failed: {error}"
            ) from error
        current = migration.version
        log.info(
            "storage.migrated",
            extra={"schema_version": migration.version, "migration": migration.name},
        )
    return current


def bootstrap(path: Path | None = None) -> Path:
    """Create or upgrade the database at `path`, then close it. Returns the path.

    Run once at core startup, before the core announces itself, so that a
    database the core cannot use is a failed start rather than a failed task.
    """
    db_path = default_db_path() if path is None else path
    conn = connect(db_path)
    try:
        version = migrate(conn)
    finally:
        conn.close()
    log.info("storage.ready", extra={"db_file": str(db_path), "schema_version": version})
    return db_path


def _check_sequence(migrations: Sequence[Migration]) -> None:
    """Versions must be exactly 1, 2, 3… — a gap or a duplicate is a build error."""
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise StorageError(
                f"migration list is out of order: expected version {expected}, "
                f"found {migration.version} ({migration.name})"
            )
