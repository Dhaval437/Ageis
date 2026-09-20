"""The budget guard (`P1-09`, `ARCHITECTURE.md § 5.3`).

`REVIEW.md § 6` asks a router for a budget-breach test. The ones that matter are the
ones where a guard could be wrong in the *cheap* direction: treating an unknown price as
free, letting a breach through because the money was spent outside a task, or aborting a
stream the user has already paid for. Each has a test here.

The ledger is real SQLite on a temp file rather than a fake, because the arithmetic
under test is the SQL.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import get_args

import pytest
from aegis_core.models.budget import (
    CEILINGS,
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
    Ceiling,
)
from aegis_core.models.schemas import Usage
from aegis_core.storage.db import bootstrap, connect
from aegis_core.storage.usage import UsageLedger
from pydantic import ValidationError

NOW = "2026-09-20T12:00:00.000Z"


class FakeHub:
    """Records what the guard published, in the hub's own shape."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object] | None, str | None]] = []

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        task_id: str | None = None,
    ) -> object:
        self.events.append((event_type, payload, task_id))
        return None


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "aegis.db"


@pytest.fixture
def ledger(db_path: Path) -> Iterator[UsageLedger]:
    bootstrap(db_path)
    with UsageLedger.open(db_path) as open_ledger:
        yield open_ledger


@pytest.fixture
def task_id(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO tasks (title, goal, status, created_at) VALUES ('t', 'g', 'RUNNING', ?)",
            (NOW,),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid
    finally:
        conn.close()


def spend(
    guard: BudgetGuard,
    cents: float | None,
    *,
    tokens: int = 100,
    task_id: int | None = None,
) -> None:
    guard.record(
        Usage(input_tokens=tokens, output_tokens=0, cost_cents=cents),
        role="planner",
        provider_id="openai",
        model="gpt-4o",
        task_id=task_id,
    )


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_the_defaults_are_a_dollar_a_task_and_ten_a_day() -> None:
    limits = BudgetLimits()
    assert limits.task_cents == 100.0
    assert limits.day_cents == 1_000.0


def test_the_token_backstops_are_out_of_reach_of_an_honest_task() -> None:
    """The backstop exists for an unpriced provider, not to stop ordinary work.

    The worst honest case is the whole step budget, each step refilling a large
    context. The ceiling has to sit above that, or it becomes the thing users meet.
    """
    limits = BudgetLimits()
    assert limits.task_tokens is not None
    assert limits.day_tokens is not None
    a_heavy_task = 40 * 128_000  # the step budget, times a large context window
    assert limits.task_tokens >= a_heavy_task
    assert limits.day_tokens >= 10 * limits.task_tokens


def test_a_small_spend_is_not_rounded_into_looking_like_its_limit(
    ledger: UsageLedger, task_id: int
) -> None:
    """0.75 cents against a 0.5 cent limit read as "$0.01 ... $0.01" on the first live run."""
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=0.5))
    spend(guard, 0.75, task_id=task_id)
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)
    assert str(raised.value) == "This task has spent $0.0075, and the limit is $0.0050."


def test_a_ceiling_of_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        BudgetLimits(task_cents=0)


def test_a_ceiling_can_be_turned_off(ledger: UsageLedger, task_id: int) -> None:
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=None, task_tokens=None))
    spend(guard, 5_000.0, task_id=task_id)
    assert guard.status(task_id).breach == "day_cents"

    unlimited = BudgetGuard(
        ledger,
        limits=BudgetLimits(task_cents=None, task_tokens=None, day_cents=None, day_tokens=None),
    )
    assert unlimited.check(task_id).breach is None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_a_fresh_task_may_spend(ledger: UsageLedger, task_id: int) -> None:
    assert BudgetGuard(ledger).check(task_id).spent is False


def test_reaching_the_task_ceiling_stops_the_next_call(ledger: UsageLedger, task_id: int) -> None:
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=10.0))
    spend(guard, 9.0, task_id=task_id)
    guard.check(task_id)  # still under

    spend(guard, 1.0, task_id=task_id)
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)

    assert raised.value.ceiling == "task_cents"
    assert raised.value.status.task.cents == pytest.approx(10.0)
    assert str(raised.value) == "This task has spent $0.10, and the limit is $0.10."


def test_the_day_ceiling_stops_a_task_that_is_itself_cheap(
    ledger: UsageLedger, task_id: int
) -> None:
    guard = BudgetGuard(ledger, limits=BudgetLimits(day_cents=10.0))
    spend(guard, 10.0, task_id=None)  # yesterday's meeting was a Test button
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)
    assert raised.value.ceiling == "day_cents"
    assert "Aegis today" in str(raised.value)


def test_a_task_ceiling_is_reported_before_the_day_ceiling(
    ledger: UsageLedger, task_id: int
) -> None:
    """Both are breached; the actionable one is named."""
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=10.0, day_cents=10.0))
    spend(guard, 20.0, task_id=task_id)
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)
    assert raised.value.ceiling == "task_cents"


def test_a_call_outside_a_task_is_still_held_to_the_day_ceiling(ledger: UsageLedger) -> None:
    guard = BudgetGuard(ledger, limits=BudgetLimits(day_cents=10.0))
    guard.check(None)
    spend(guard, 10.0, task_id=None)
    with pytest.raises(BudgetExceededError):
        guard.check(None)


def test_a_task_ceiling_cannot_be_breached_by_a_call_with_no_task(ledger: UsageLedger) -> None:
    """There is no task to have overspent, so only the day ceiling applies."""
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=1.0))
    spend(guard, 50.0, task_id=None)
    assert guard.check(None).breach is None


# ---------------------------------------------------------------------------
# An unknown price is never free
# ---------------------------------------------------------------------------


def test_an_unpriced_provider_cannot_spend_forever(ledger: UsageLedger, task_id: int) -> None:
    """The cents total never moves, so the token backstop is the only thing that fires."""
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=10.0, task_tokens=1_000))
    spend(guard, None, tokens=999, task_id=task_id)
    assert guard.check(task_id).breach is None

    spend(guard, None, tokens=1, task_id=task_id)
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)
    assert raised.value.ceiling == "task_tokens"
    assert str(raised.value) == "This task has used 1,000 tokens, and the limit is 1,000 tokens."
    assert raised.value.status.task.cents == 0.0


def test_a_total_that_is_missing_a_price_says_so(ledger: UsageLedger, task_id: int) -> None:
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=10.0))
    spend(guard, None, task_id=task_id)
    spend(guard, 10.0, task_id=task_id)
    with pytest.raises(BudgetExceededError) as raised:
        guard.check(task_id)
    assert raised.value.status.task.unpriced_calls == 1
    assert "at least $0.10" in str(raised.value)


# ---------------------------------------------------------------------------
# Recording, and the live cost event
# ---------------------------------------------------------------------------


def test_recording_a_call_publishes_cost_updated(ledger: UsageLedger, task_id: int) -> None:
    hub = FakeHub()
    guard = BudgetGuard(ledger, publisher=hub)
    guard.record(
        Usage(input_tokens=1_000, output_tokens=500, cost_cents=4.5),
        role="grounder",
        provider_id="anthropic",
        model="claude-sonnet-4",
        task_id=task_id,
    )

    (event_type, payload, event_task_id) = hub.events[0]
    assert event_type == "cost.updated"
    assert event_task_id == str(task_id)
    assert payload is not None
    assert payload["call"] == {
        "role": "grounder",
        "provider_id": "anthropic",
        "model": "claude-sonnet-4",
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cost_cents": 4.5,
    }
    assert payload["task"] == {"cents": 4.5, "tokens": 1_500, "unpriced_calls": 0}
    assert payload["day"] == {"cents": 4.5, "tokens": 1_500, "unpriced_calls": 0}
    assert payload["breach"] is None


def test_the_event_carries_the_limits_so_a_meter_can_be_drawn(
    ledger: UsageLedger, task_id: int
) -> None:
    hub = FakeHub()
    limits = BudgetLimits(task_cents=50.0)
    spend(BudgetGuard(ledger, limits=limits, publisher=hub), 1.0, task_id=task_id)
    payload = hub.events[0][1]
    assert payload is not None
    assert payload["limits"] == {
        "task_cents": 50.0,
        "day_cents": limits.day_cents,
        "task_tokens": limits.task_tokens,
        "day_tokens": limits.day_tokens,
    }


def test_the_event_says_when_a_ceiling_has_been_reached(ledger: UsageLedger, task_id: int) -> None:
    """The UI pauses on this, before the next call is even attempted."""
    hub = FakeHub()
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=1.0), publisher=hub)
    spend(guard, 5.0, task_id=task_id)
    payload = hub.events[0][1]
    assert payload is not None
    assert payload["breach"] == "task_cents"


def test_recording_a_breach_does_not_raise(ledger: UsageLedger, task_id: int) -> None:
    """The money is already spent; the refusal belongs at the start of the next call."""
    guard = BudgetGuard(ledger, limits=BudgetLimits(task_cents=1.0))
    status = guard.record(
        Usage(input_tokens=10, output_tokens=0, cost_cents=99.0),
        role="planner",
        provider_id="openai",
        model="gpt-4o",
        task_id=task_id,
    )
    assert status.breach == "task_cents"


def test_a_guard_with_no_publisher_still_records(ledger: UsageLedger, task_id: int) -> None:
    guard = BudgetGuard(ledger)
    spend(guard, 1.0, task_id=task_id)
    assert guard.status(task_id).task.cents == pytest.approx(1.0)


def test_an_event_payload_carries_nothing_but_numbers_and_ids(
    ledger: UsageLedger, task_id: int
) -> None:
    """A `cost.updated` reaches the renderer. Nothing a key or a prompt could be in."""
    hub = FakeHub()
    spend(BudgetGuard(ledger, publisher=hub), 1.0, task_id=task_id)
    payload = hub.events[0][1]
    assert payload is not None
    assert set(payload) == {"task", "day", "limits", "breach", "call"}
    call = payload["call"]
    assert isinstance(call, dict)
    assert set(call) == {
        "role",
        "provider_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_cents",
    }


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_every_ceiling_is_checked() -> None:
    """A ceiling added to the type and forgotten in the check is a ceiling that is not one."""
    assert set(CEILINGS) == set(BudgetLimits.model_fields)
    assert set(CEILINGS) == set(get_args(Ceiling))
