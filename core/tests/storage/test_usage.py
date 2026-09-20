"""The usage ledger (`P1-09`, `storage/usage.py` + `m0002_usage.py`).

What matters here is arithmetic that must never be wrong in the cheap direction: an
unknown price is never counted as zero, a day is the user's day and not UTC's, and a
total survives the app being restarted — a per-day ceiling that a relaunch clears is not
a ceiling.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from aegis_core.models.router import ROLES
from aegis_core.storage.db import StorageError, bootstrap, connect, migrate, schema_version
from aegis_core.storage.migrations import MIGRATIONS
from aegis_core.storage.usage import (
    Spend,
    UsageEntry,
    UsageLedger,
    UsageRole,
    local_day,
    utc_timestamp,
)
from pydantic import ValidationError

NOW = "2026-09-20T12:00:00.000Z"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "Dhaval's data ü" / "aegis.db"


@pytest.fixture
def ledger(db_path: Path) -> Iterator[UsageLedger]:
    bootstrap(db_path)
    with UsageLedger.open(db_path) as open_ledger:
        yield open_ledger


def an_entry(**overrides: object) -> UsageEntry:
    base: dict[str, object] = {
        "role": "planner",
        "provider_id": "openai",
        "model": "gpt-4o",
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cost_cents": 4.5,
    }
    return UsageEntry(**(base | overrides))  # type: ignore[arg-type]


def add_task(path: Path) -> int:
    conn = connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO tasks (title, goal, status, created_at) VALUES ('t', 'g', 'RUNNING', ?)",
            (NOW,),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_role_vocabulary_matches_the_router() -> None:
    """`UsageRole` is declared in storage so it does not import the router. It must agree."""
    assert set(get_args(UsageRole)) == set(ROLES)


def test_the_check_constraint_refuses_an_unknown_role(db_path: Path) -> None:
    bootstrap(db_path)
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage (ts, day, role, provider_id, model, input_tokens,"
                " output_tokens) VALUES (?, '2026-09-20', 'architect', 'openai', 'm', 1, 1)",
                (NOW,),
            )
    finally:
        conn.close()


def test_a_negative_token_count_is_refused(db_path: Path) -> None:
    bootstrap(db_path)
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage (ts, day, role, provider_id, model, input_tokens,"
                " output_tokens) VALUES (?, '2026-09-20', 'planner', 'openai', 'm', -1, 1)",
                (NOW,),
            )
    finally:
        conn.close()


def test_a_row_for_a_task_that_does_not_exist_is_refused(ledger: UsageLedger) -> None:
    with pytest.raises(StorageError, match="cannot record model usage"):
        ledger.record(an_entry(task_id=4242))


def test_deleting_a_task_takes_its_usage_with_it(db_path: Path, ledger: UsageLedger) -> None:
    task_id = add_task(db_path)
    ledger.record(an_entry(task_id=task_id))
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        assert conn.execute("SELECT count(*) FROM usage").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def test_an_empty_ledger_has_spent_nothing(ledger: UsageLedger) -> None:
    assert ledger.day_spend() == Spend()
    assert ledger.task_spend(1) == Spend()


def test_totals_add_up(db_path: Path, ledger: UsageLedger) -> None:
    task_id = add_task(db_path)
    ledger.record(an_entry(task_id=task_id))
    ledger.record(an_entry(task_id=task_id, cost_cents=1.5, input_tokens=10, output_tokens=5))

    spend = ledger.task_spend(task_id)
    assert spend.cents == pytest.approx(6.0)
    assert spend.tokens == 1_515
    assert spend.unpriced_calls == 0
    assert spend.complete


def test_a_task_total_excludes_other_tasks(db_path: Path, ledger: UsageLedger) -> None:
    mine, theirs = add_task(db_path), add_task(db_path)
    ledger.record(an_entry(task_id=mine))
    ledger.record(an_entry(task_id=theirs))
    assert ledger.task_spend(mine).cents == pytest.approx(4.5)
    assert ledger.day_spend().cents == pytest.approx(9.0)


def test_a_day_total_includes_calls_made_outside_any_task(ledger: UsageLedger) -> None:
    """A `P1-10` Test button spends real money with no task to charge it to."""
    ledger.record(an_entry(task_id=None))
    assert ledger.day_spend().tokens == 1_500


# ---------------------------------------------------------------------------
# An unknown price is never free
# ---------------------------------------------------------------------------


def test_an_unknown_price_is_counted_as_unknown_not_as_zero(ledger: UsageLedger) -> None:
    ledger.record(an_entry(cost_cents=None))
    ledger.record(an_entry(cost_cents=2.0))

    spend = ledger.day_spend()
    assert spend.cents == pytest.approx(2.0)
    assert spend.unpriced_calls == 1
    assert not spend.complete
    # The tokens are still counted, which is what the token ceiling is for.
    assert spend.tokens == 3_000


def test_a_local_model_is_free_and_that_is_a_known_price(ledger: UsageLedger) -> None:
    ledger.record(an_entry(provider_id="ollama", model="llama3.1:8b", cost_cents=0.0))
    spend = ledger.day_spend()
    assert spend.cents == 0.0
    assert spend.unpriced_calls == 0


def test_an_entry_refuses_a_negative_cost() -> None:
    with pytest.raises(ValidationError):
        an_entry(cost_cents=-1.0)


# ---------------------------------------------------------------------------
# Days
# ---------------------------------------------------------------------------


def test_the_day_is_the_users_day_not_utc() -> None:
    """08:00 on the 20th in Sydney is the 20th, though UTC is still calling it the 19th."""
    sydney = datetime(2026, 9, 20, 8, 0, tzinfo=timezone(timedelta(hours=11)))
    assert local_day(sydney) == "2026-09-20"
    assert sydney.astimezone(UTC).date().isoformat() == "2026-09-19"

    # And the other way: 03:00 UTC on the 20th is still the 19th in Los Angeles.
    utc_night = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
    assert local_day(utc_night.astimezone(timezone(timedelta(hours=-7)))) == "2026-09-19"


def test_yesterdays_spending_does_not_count_against_today(ledger: UsageLedger) -> None:
    yesterday = datetime.now().astimezone() - timedelta(days=1)
    ledger.record(an_entry(), moment=yesterday)
    ledger.record(an_entry())

    assert ledger.day_spend().cents == pytest.approx(4.5)
    assert ledger.day_spend(local_day(yesterday)).cents == pytest.approx(4.5)


def test_a_timestamp_is_utc_with_milliseconds() -> None:
    stamped = utc_timestamp(datetime(2026, 9, 20, 22, 0, tzinfo=timezone(timedelta(hours=11))))
    assert stamped == "2026-09-20T11:00:00.000+00:00"


# ---------------------------------------------------------------------------
# Durability and lifetime
# ---------------------------------------------------------------------------


def test_a_total_survives_a_restart(db_path: Path) -> None:
    """The point of putting it on disk: a relaunch must not clear a day ceiling."""
    bootstrap(db_path)
    with UsageLedger.open(db_path) as first:
        first.record(an_entry())
    with UsageLedger.open(db_path) as second:
        assert second.day_spend().cents == pytest.approx(4.5)


def test_the_ledger_can_be_written_from_another_thread(ledger: UsageLedger) -> None:
    """The hub already allows a publisher off the event loop; so must this."""
    errors: list[BaseException] = []

    def write() -> None:
        try:
            ledger.record(an_entry())
        except BaseException as error:  # pragma: no cover — reported, not swallowed
            errors.append(error)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert ledger.day_spend().tokens == 12_000


def test_a_closed_ledger_refuses_to_work(db_path: Path) -> None:
    bootstrap(db_path)
    closed = UsageLedger.open(db_path)
    closed.close()
    closed.close()  # idempotent
    with pytest.raises(StorageError, match="closed"):
        closed.record(an_entry())
    with pytest.raises(StorageError, match="closed"):
        closed.day_spend()


def test_a_database_from_before_the_ledger_is_upgraded_in_place(db_path: Path) -> None:
    """Every existing install is at version 1. Its tasks must survive the upgrade."""
    conn = connect(db_path)
    try:
        migrate(conn, MIGRATIONS[:1])
        assert schema_version(conn) == 1
        conn.execute(
            "INSERT INTO tasks (title, goal, status, created_at) VALUES ('old', 'g', 'DONE', ?)",
            (NOW,),
        )
    finally:
        conn.close()

    bootstrap(db_path)

    conn = connect(db_path)
    try:
        assert schema_version(conn) == MIGRATIONS[-1].version
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "old"
    finally:
        conn.close()
    with UsageLedger.open(db_path) as ledger:
        ledger.record(an_entry(task_id=1))
        assert ledger.task_spend(1).cents == pytest.approx(4.5)


def test_open_does_not_migrate(db_path: Path) -> None:
    """`bootstrap()` owns the schema. A ledger opened on an un-migrated file fails."""
    connect(db_path).close()
    with UsageLedger.open(db_path) as fresh, pytest.raises(StorageError):
        fresh.record(an_entry())
