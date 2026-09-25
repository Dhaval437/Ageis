"""Tests for opening `aegis.db` and running its migrations (P0-10)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
from aegis_core.server.schemas import Decision, RiskTier, TaskState
from aegis_core.storage.db import StorageError, bootstrap, connect, migrate, schema_version
from aegis_core.storage.migrations import MIGRATIONS, Migration
from aegis_core.storage.migrations import m0001_initial as m0001

#: `ARCHITECTURE.md § 7`, plus `forward_json`/`created_at` on `journal` from
#: `RECOVERY.md § 2.1`. A column dropped or renamed by accident fails here.
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "tasks": [
        "id", "title", "goal", "status", "created_at", "ended_at",
        "model_map_json", "cost_cents", "step_count",
    ],
    "steps": [
        "id", "task_id", "idx", "phase", "thought", "tool", "params_json", "risk",
        "decision", "approved_by", "started_at", "ended_at", "ok", "error",
        "observation_ref",
    ],
    "observations": ["id", "task_id", "step_id", "kind", "path", "phash", "meta_json"],
    "approvals": ["id", "step_id", "prompt", "choice", "remembered", "decided_at"],
    "audit": ["id", "ts", "actor", "event", "payload_json", "prev_hash", "hash"],
    "facts": ["id", "scope", "key", "value", "source_task_id", "created_at"],
    "journal": [
        "id", "task_id", "step_id", "op", "forward_json", "undo_json", "applied",
        "created_at", "undone_at",
    ],
    "settings": ["key", "value_json"],
    "usage": [
        "id", "task_id", "ts", "day", "role", "provider_id", "model",
        "input_tokens", "output_tokens", "cost_cents",
    ],
}  # fmt: skip

NOW = "2026-09-14T12:00:00.000Z"
HASH = "a" * 64

#: One past the last shipped migration. Spelled this way so adding a migration does not
#: silently turn the "only pending migrations run" tests into tests of nothing.
NEXT_VERSION = MIGRATIONS[-1].version + 1


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # A space and non-ASCII in the path: `REVIEW.md § 2` asks for both.
    return tmp_path / "Dhaval's data ü" / "aegis.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


def _add_task(conn: sqlite3.Connection, status: str = "RUNNING") -> int:
    cursor = conn.execute(
        "INSERT INTO tasks (title, goal, status, created_at) VALUES ('t', 'g', ?, ?)",
        (status, NOW),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _add_step(conn: sqlite3.Connection, task_id: int, idx: int = 0) -> int:
    cursor = conn.execute(
        "INSERT INTO steps (task_id, idx, phase, started_at) VALUES (?, ?, 'plan', ?)",
        (task_id, idx, NOW),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


def test_connect_creates_the_folder_and_the_file(db_path: Path) -> None:
    connect(db_path).close()
    assert db_path.is_file()


def test_wal_mode_is_on(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_wal_mode_persists_for_a_plain_connection(db_path: Path) -> None:
    connect(db_path).close()
    plain = sqlite3.connect(db_path)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()


def test_writes_are_fully_synchronous(conn: sqlite3.Connection) -> None:
    """Invariant 11: a journal row must survive a power cut once committed. 2 = FULL."""
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_step(conn, task_id=999)


def test_connections_start_outside_a_transaction(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO settings VALUES ('theme', '\"dark\"')")
    assert not conn.in_transaction


def test_an_unopenable_path_is_a_storage_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(StorageError):
        connect(blocker / "aegis.db")


def test_a_non_database_file_is_a_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "aegis.db"
    path.write_bytes(b"this is not a database, it is " * 200)
    with pytest.raises(StorageError):
        connect(path)


def test_an_old_sqlite_is_refused(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 31, 1))
    with pytest.raises(StorageError, match="too old"):
        connect(db_path)
    assert not db_path.exists()


# --------------------------------------------------------------------------- #
# Migrating
# --------------------------------------------------------------------------- #


def test_migrations_are_numbered_from_one_without_gaps() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


def test_a_fresh_database_reaches_the_latest_version(conn: sqlite3.Connection) -> None:
    assert schema_version(conn) == MIGRATIONS[-1].version


def test_the_schema_matches_the_data_model(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == set(EXPECTED_COLUMNS)
    for table, columns in EXPECTED_COLUMNS.items():
        actual = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        assert actual == columns, table


def test_every_table_is_strict(conn: sqlite3.Connection) -> None:
    strict = {row[1]: row[5] for row in conn.execute("PRAGMA table_list") if row[0] == "main"}
    for table in EXPECTED_COLUMNS:
        assert strict[table] == 1, table


def test_migrating_twice_changes_nothing(db_path: Path) -> None:
    bootstrap(db_path)
    conn = connect(db_path)
    try:
        before = conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall()
        assert migrate(conn) == MIGRATIONS[-1].version
        after = conn.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall()
    finally:
        conn.close()
    assert before == after


def test_only_pending_migrations_run(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        extra = Migration(
            NEXT_VERSION, "add_notes", "CREATE TABLE notes (id INTEGER PRIMARY KEY) STRICT;"
        )
        assert migrate(conn, (*MIGRATIONS, extra)) == NEXT_VERSION
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_failing_migration_leaves_no_trace(db_path: Path) -> None:
    """The DDL and the version bump commit together or not at all."""
    broken = Migration(
        NEXT_VERSION,
        "half_done",
        "CREATE TABLE half (id INTEGER PRIMARY KEY) STRICT;\nCREATE TABLE tasks (x INTEGER);",
    )
    conn = connect(db_path)
    try:
        migrate(conn)
        with pytest.raises(StorageError, match=f"migration {NEXT_VERSION}"):
            migrate(conn, (*MIGRATIONS, broken))
        assert schema_version(conn) == MIGRATIONS[-1].version
        assert not conn.in_transaction
        exists = conn.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'half'")
        assert exists.fetchone()[0] == 0
    finally:
        conn.close()


def test_a_database_newer_than_this_build_is_refused(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(StorageError, match="newer"):
            migrate(conn)
        assert schema_version(conn) == 99
    finally:
        conn.close()


@pytest.mark.parametrize(
    "versions",
    [(2,), (1, 3), (1, 1)],
    ids=["starts-at-two", "gap", "duplicate"],
)
def test_a_misnumbered_migration_list_is_refused(db_path: Path, versions: tuple[int, ...]) -> None:
    migrations = [Migration(v, f"m{v}", "SELECT 1;") for v in versions]
    conn = connect(db_path)
    try:
        with pytest.raises(StorageError, match="out of order"):
            migrate(conn, migrations)
        assert schema_version(conn) == 0
    finally:
        conn.close()


def test_bootstrap_uses_localappdata_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = bootstrap()
    assert path == tmp_path / "Aegis" / "aegis.db"
    assert path.is_file()


# --------------------------------------------------------------------------- #
# Constraints the schema promises
# --------------------------------------------------------------------------- #


def test_the_audit_log_rejects_updates(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO audit (ts, actor, event, payload_json, prev_hash, hash) "
        "VALUES (?, 'core', 'task.created', '{}', ?, ?)",
        (NOW, "0" * 64, HASH),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit SET event = 'nothing happened'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit")
    assert conn.execute("SELECT event FROM audit").fetchone()[0] == "task.created"


def test_an_unknown_task_status_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_task(conn, status="ALMOST_DONE")


def test_the_task_status_check_is_exactly_the_wire_vocabulary() -> None:
    # `tasks.status` and the `task.status` event carry the same words, and MAIN's
    # watchdog decides on them: a state one side knows and the other refuses is a
    # task the watchdog cannot see.
    match = re.search(r"status\s+TEXT\s+NOT NULL CHECK \(status IN \(([^)]*)\)\)", m0001.SQL)
    assert match is not None
    checked = re.findall(r"'([A-Z_]+)'", match.group(1))
    assert checked == list(get_args(TaskState))


@pytest.mark.parametrize(
    ("column", "vocabulary"),
    [("risk", get_args(RiskTier)), ("decision", get_args(Decision))],
)
def test_the_step_checks_are_exactly_the_guardians_vocabulary(
    column: str, vocabulary: tuple[str, ...]
) -> None:
    # `steps.risk` / `steps.decision` record what `Guardian.evaluate()` returned (P3-08).
    match = re.search(rf"{column}\s+TEXT\s+CHECK \({column} IN \(([^)]*)\)\)", m0001.SQL)
    assert match is not None
    assert re.findall(r"'([A-Za-z_]+)'", match.group(1)) == list(vocabulary)


@pytest.mark.parametrize("status", get_args(TaskState))
def test_every_task_state_is_accepted(conn: sqlite3.Connection, status: str) -> None:
    _add_task(conn, status=status)


def test_an_unknown_risk_tier_is_rejected(conn: sqlite3.Connection) -> None:
    task = _add_task(conn)
    step = _add_step(conn, task)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE steps SET risk = 'PROBABLY_FINE' WHERE id = ?", (step,))


def test_invalid_json_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO settings VALUES ('theme', 'dark')")


def test_strict_tables_do_not_coerce_types(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (title, goal, status, created_at, step_count) "
            "VALUES ('t', 'g', 'QUEUED', ?, 'three')",
            (NOW,),
        )


def test_a_decided_approval_needs_a_decision_time(conn: sqlite3.Connection) -> None:
    step = _add_step(conn, _add_task(conn))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approvals (step_id, prompt, choice) VALUES (?, 'Delete?', 'deny')",
            (step,),
        )
    conn.execute("INSERT INTO approvals (step_id, prompt) VALUES (?, 'Delete?')", (step,))


def test_step_indexes_are_unique_within_a_task(conn: sqlite3.Connection) -> None:
    task = _add_task(conn)
    _add_step(conn, task, idx=0)
    with pytest.raises(sqlite3.IntegrityError):
        _add_step(conn, task, idx=0)
    _add_step(conn, _add_task(conn), idx=0)


def test_a_journal_row_starts_unapplied(conn: sqlite3.Connection) -> None:
    """Invariant 11: the row exists, marked not-yet-applied, before the action runs."""
    task = _add_task(conn)
    conn.execute(
        "INSERT INTO journal (task_id, op, forward_json, created_at) "
        'VALUES (?, \'fs.move\', \'{"src": "a", "dst": "b"}\', ?)',
        (task, NOW),
    )
    assert conn.execute("SELECT applied FROM journal").fetchone()[0] == 0
