"""What every model call cost, and what a task and a day have spent so far.

This is the durable half of the budget guard (`P1-09`). The guard decides; this
remembers. It is deliberately dumb: it appends one row per call and answers two sums.

**Why it is on disk at all.** A per-day ceiling that an app restart clears is not a
ceiling — it is a suggestion that a crash loop can spend past. So the totals come from
`usage` (`m0002_usage.py`), and the day the ceiling counts is the **local** one, because
a promise about "today" is a promise about the user's day.

**What it never does is make a price up.** `cost_cents` is `NULL` for a call whose price
this build does not know, and that is carried all the way out as `Spend.unpriced_calls`
rather than being summed in as zero (`models/schemas.py`, `Usage`). A total that is
quietly short is worse than a total that says it is incomplete.

This is the core's first long-lived database connection. It owns one, opened
`cross_thread` and serialised under a lock, because `publish`-side callers are not all
on the event loop (`server/hub.py` makes the same allowance) and one connection under a
lock is cheaper than a pool for a table written once per model call.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from aegis_core.models.schemas import ProviderId
from aegis_core.storage.db import StorageError, connect, default_db_path

log = logging.getLogger(__name__)

UsageRole = Literal["planner", "grounder", "utility"]
"""The three roles of `ARCHITECTURE.md § 5.2`, spelled as the `usage.role` CHECK does.

Declared here rather than imported from `models/router.py`: storage may know the
vocabulary a column is constrained to (the same argument as `ProviderId` in
`storage/vault.py`), but it must not depend on the router. A test asserts the two lists
agree, so they cannot drift.
"""

_INSERT: Final = """
INSERT INTO usage (task_id, ts, day, role, provider_id, model, input_tokens,
                   output_tokens, cost_cents)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: Sums for one slice of the ledger. `SUM` over no rows is NULL, hence the coalesces;
#: `cost_cents IS NULL` is counted, never coerced to 0.
_TOTALS: Final = """
SELECT COALESCE(SUM(cost_cents), 0.0),
       COALESCE(SUM(input_tokens + output_tokens), 0),
       COALESCE(SUM(cost_cents IS NULL), 0)
FROM usage
"""


def local_day(moment: datetime | None = None) -> str:
    """Today's date where the user is, as `YYYY-MM-DD`.

    A `moment` is read in its own zone rather than converted: a naive one is local by
    definition, and an aware one already names the offset the date should be read in.
    The default is this machine's now, which is the user's day.
    """
    return (datetime.now().astimezone() if moment is None else moment).date().isoformat()


def utc_timestamp(moment: datetime | None = None) -> str:
    """UTC ISO-8601 with milliseconds — the same spelling as a stream event's `ts`."""
    now = datetime.now(tz=UTC) if moment is None else moment.astimezone(UTC)
    return now.isoformat(timespec="milliseconds")


class Spend(BaseModel):
    """What some slice of the ledger adds up to.

    `cents` is the sum of the prices that are **known**. `unpriced_calls` is how many
    calls it therefore leaves out, so a caller can say "at least this much" rather than
    presenting a short number as a complete one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cents: float = Field(default=0.0, ge=0)
    tokens: int = Field(default=0, ge=0)
    unpriced_calls: int = Field(default=0, ge=0)

    @property
    def complete(self) -> bool:
        """Every call in this total had a known price."""
        return self.unpriced_calls == 0


class UsageEntry(BaseModel):
    """One model call, as it goes into the ledger.

    Carries no message, no prompt and no key — only who answered and how much of it
    there was. `cost_cents` is `None` when the price is unknown and is never 0 for that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: UsageRole
    provider_id: ProviderId
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_cents: float | None = Field(default=None, ge=0)
    task_id: int | None = Field(default=None, description="`None` for a call outside a task.")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageLedger:
    """One connection, one lock, one table. Append and two sums."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, path: Path | None = None) -> Self:
        """Open the ledger on `aegis.db`.

        The schema is not migrated here: `storage.db.bootstrap()` has already run at
        startup, before the core announced itself, so a database this build cannot use
        is a failed start rather than a failed task (`P0-10`).
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

    def record(self, entry: UsageEntry, *, moment: datetime | None = None) -> None:
        """Append one call. Raises `StorageError` — an unaccountable spend is not ignored."""
        row = (
            entry.task_id,
            utc_timestamp(moment),
            local_day(moment),
            entry.role,
            entry.provider_id,
            entry.model,
            entry.input_tokens,
            entry.output_tokens,
            entry.cost_cents,
        )
        with self._lock:
            self._require_open()
            try:
                self._conn.execute(_INSERT, row)
            except sqlite3.Error as error:
                raise StorageError(f"cannot record model usage: {error}") from error

    def task_spend(self, task_id: int) -> Spend:
        """What one task has spent."""
        return self._totals("WHERE task_id = ?", (task_id,))

    def day_spend(self, day: str | None = None) -> Spend:
        """What one local date has spent, across every task and outside them all."""
        return self._totals("WHERE day = ?", (local_day() if day is None else day,))

    def _totals(self, where: str, params: tuple[object, ...]) -> Spend:
        with self._lock:
            self._require_open()
            try:
                row = self._conn.execute(f"{_TOTALS}\n{where}", params).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read model usage: {error}") from error
        cents, tokens, unpriced = row
        return Spend(cents=cents, tokens=tokens, unpriced_calls=unpriced)

    def _require_open(self) -> None:
        """Caller holds `_lock`."""
        if self._closed:
            raise StorageError("the usage ledger is closed")
