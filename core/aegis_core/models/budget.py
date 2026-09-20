"""The budget guard — whether to ask at all (`ARCHITECTURE.md § 5.3`).

`models/router.py` decides *who answers*. This decides *whether the question gets
asked*, and it is the module that makes § 5.3's last promise true: **on breach, pause
and ask the user; never silently continue.**

Three decisions shape it.

**A breach is refused at the door, never mid-flight.** Every adapter reports usage once,
at the end of a stream — by the time a call's cost is known, it has been spent, and
aborting the last delta of a paid-for answer throws away what the user already bought
while leaving the timeline half-written. So `check()` refuses *before* a call, and
`record()` never raises on a breach: it writes the call, publishes `cost.updated` with
`breach` set, and the next `check()` is the one that stops. `P4` sees a task that runs
out of budget between steps, which is exactly where a pause belongs.

**There are token ceilings as well as cost ceilings**, because a cents-only guard is no
guard at all on the providers that need one most. An unknown price is `None` and is
never summed as zero (`P1-05`, `P1-06`), so a `custom` gateway or an unlisted OpenRouter
model can run all day without moving a cents total by a penny. The token ceilings are
the backstop for exactly that case — an amount no honest task reaches, so that on a
priced provider the cents ceiling is what a user meets, and on an unpriced one there is
still something to meet.

**The guard never guesses what a call will cost.** It knows what has been spent, not
what is about to be. A ceiling therefore stops the call *after* the one that crossed it,
and the ledger's `unpriced_calls` is carried into every event so the UI can say a total
is incomplete rather than presenting a short number as a complete one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aegis_core.models.schemas import ProviderId, Usage
from aegis_core.server.schemas import EventType
from aegis_core.storage.usage import Spend, UsageEntry, UsageLedger, UsageRole

log = logging.getLogger(__name__)

#: The default per-task ceiling: one dollar. A 40-step task (`ARCHITECTURE.md § 6.1`)
#: with one downscaled screenshot per step lands well inside it on a mid-range model and
#: trips it on a frontier one — which is the point. The user raises it in Models.
DEFAULT_TASK_CENTS: Final = 100.0

#: The default per-day ceiling: ten dollars.
DEFAULT_DAY_CENTS: Final = 1_000.0

#: The backstops for a provider whose price this build does not know. A 40-step task
#: (`ARCHITECTURE.md § 6.1`) that refilled a 128k context every step would spend about
#: five million tokens, so this is twice what an honest task reaches — and is the only
#: thing standing between an unpriced gateway and an unbounded run.
DEFAULT_TASK_TOKENS: Final = 10_000_000
DEFAULT_DAY_TOKENS: Final = 100_000_000

Ceiling = Literal["task_cents", "task_tokens", "day_cents", "day_tokens"]
"""Which ceiling was reached. Checked in this order: a task ceiling is the one the user
can act on, so it is reported before the day ceiling it also implies."""

CEILINGS: Final[tuple[Ceiling, ...]] = ("task_cents", "task_tokens", "day_cents", "day_tokens")


class BudgetError(Exception):
    """The budget guard stopped something. Shown to the user, so it names no key."""


class BudgetExceededError(BudgetError):
    """A ceiling has been reached. The task pauses; it does not continue.

    `status` is the whole picture at the moment of refusal — both totals, the limits,
    and which ceiling it was — so the UI can show spend against the ceiling without
    asking anything else (`UI.md § 9`, *Budget exceeded*).
    """

    def __init__(self, status: BudgetStatus) -> None:
        super().__init__(status.message)
        self.status = status
        self.ceiling: Ceiling = status.require_breach()


class BudgetLimits(BaseModel):
    """The ceilings, from Settings. `None` is *no ceiling on this one*.

    Defaults exist so a guard is usable before anything has been configured; the Models
    screen (`P1-10`) is what lets a user change them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_cents: float | None = Field(default=DEFAULT_TASK_CENTS, gt=0)
    day_cents: float | None = Field(default=DEFAULT_DAY_CENTS, gt=0)
    task_tokens: int | None = Field(default=DEFAULT_TASK_TOKENS, gt=0)
    day_tokens: int | None = Field(default=DEFAULT_DAY_TOKENS, gt=0)

    def limit(self, ceiling: Ceiling) -> float | None:
        return getattr(self, ceiling)  # type: ignore[no-any-return]


class BudgetStatus(BaseModel):
    """Where the money stands for one task, right now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: Spend
    day: Spend
    limits: BudgetLimits
    breach: Ceiling | None = None
    task_id: int | None = None

    @property
    def spent(self) -> bool:
        """A ceiling has been reached. Nothing further may be asked of a model."""
        return self.breach is not None

    def require_breach(self) -> Ceiling:
        if self.breach is None:  # pragma: no cover — only reached through a breach
            raise ValueError("no ceiling was reached")
        return self.breach

    @property
    def message(self) -> str:
        """What the user is told. Plain, second person, no softening (`UI.md § 11`)."""
        if self.breach is None:
            return "Spending is within your limits."
        limit = self.limits.limit(self.breach)
        if limit is None:  # pragma: no cover — a breach implies a limit
            raise ValueError("a ceiling was reached with no limit set")
        spend = self.task if self.breach.startswith("task") else self.day
        subject = "This task" if self.breach.startswith("task") else "Aegis today"
        if self.breach.endswith("cents"):
            about = "" if spend.complete else "at least "
            return (
                f"{subject} has spent {about}{_money(spend.cents)}, "
                f"and the limit is {_money(limit)}."
            )
        return (
            f"{subject} has used {spend.tokens:,} tokens, and the limit is {int(limit):,} tokens."
        )

    def payload(self) -> dict[str, JsonValue]:
        """The body of a `cost.updated` event (`ARCHITECTURE.md § 9.2`).

        Numbers and model ids only: no message, no prompt, no path, nothing a key could
        be in. It is what `SpendMeter` (`UI.md § 12`) and the budget card render.
        """
        return {
            "task": _spend_json(self.task),
            "day": _spend_json(self.day),
            "limits": {
                "task_cents": self.limits.task_cents,
                "day_cents": self.limits.day_cents,
                "task_tokens": self.limits.task_tokens,
                "day_tokens": self.limits.day_tokens,
            },
            "breach": self.breach,
        }


class EventPublisher(Protocol):
    """The one thing the guard needs from the event hub.

    Structural, so `server.hub.EventHub` satisfies it without the model layer importing
    the server — the same shape as `router.KeySource` over the vault.
    """

    def publish(
        self,
        event_type: EventType,
        payload: Mapping[str, JsonValue] | None = ...,
        *,
        task_id: str | None = ...,
    ) -> object: ...


class BudgetGuard:
    """Reads the ledger, compares it to the ceilings, and says yes or no."""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        limits: BudgetLimits | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._ledger = ledger
        self._limits = limits if limits is not None else BudgetLimits()
        self._publisher = publisher

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def status(self, task_id: int | None = None) -> BudgetStatus:
        """Both totals and whether either has reached a ceiling. Reads, never writes."""
        task = self._ledger.task_spend(task_id) if task_id is not None else Spend()
        day = self._ledger.day_spend()
        return BudgetStatus(
            task=task,
            day=day,
            limits=self._limits,
            breach=self._breach(task, day, has_task=task_id is not None),
            task_id=task_id,
        )

    def check(self, task_id: int | None = None) -> BudgetStatus:
        """Refuse the call that is about to be made, if a ceiling has been reached.

        Called before anything reaches the wire. Raises `BudgetExceededError`; the task
        pauses and asks the user (`ARCHITECTURE.md § 5.3`), and the only ways on are
        raising the limit or stopping (`UI.md § 9`).
        """
        status = self.status(task_id)
        if status.spent:
            log.warning(
                "model.budget_exceeded",
                extra={
                    "task_id": task_id,
                    "ceiling": status.breach,
                    "task_cents": status.task.cents,
                    "day_cents": status.day.cents,
                },
            )
            raise BudgetExceededError(status)
        return status

    def record(
        self,
        usage: Usage,
        *,
        role: UsageRole,
        provider_id: ProviderId,
        model: str,
        task_id: int | None = None,
    ) -> BudgetStatus:
        """Write one completed call to the ledger and publish `cost.updated`.

        Never raises on a breach — see the module docstring. It does raise if the write
        itself fails (`StorageError`): a spend that could not be accounted for must not
        pass silently, because everything after it would be counted against a total that
        is quietly short.
        """
        entry = UsageEntry(
            role=role,
            provider_id=provider_id,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_cents=usage.cost_cents,
            task_id=task_id,
        )
        self._ledger.record(entry)
        status = self.status(task_id)
        log.info(
            "model.cost",
            extra={
                "task_id": task_id,
                "role": role,
                "provider_id": provider_id,
                "model": model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_cents": usage.cost_cents,
                "breach": status.breach,
            },
        )
        self._publish(status, entry)
        return status

    def _publish(self, status: BudgetStatus, entry: UsageEntry) -> None:
        if self._publisher is None:
            return
        payload = status.payload()
        payload["call"] = {
            "role": entry.role,
            "provider_id": entry.provider_id,
            "model": entry.model,
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "cost_cents": entry.cost_cents,
        }
        # `StreamEvent.task_id` is a string; the ledger's is the `tasks.id` integer.
        task_id = None if status.task_id is None else str(status.task_id)
        self._publisher.publish("cost.updated", payload, task_id=task_id)

    def _breach(self, task: Spend, day: Spend, *, has_task: bool) -> Ceiling | None:
        """The first ceiling reached, in `CEILINGS` order, or `None`."""
        totals: dict[Ceiling, float] = {
            "task_cents": task.cents,
            "task_tokens": task.tokens,
            "day_cents": day.cents,
            "day_tokens": day.tokens,
        }
        for ceiling in CEILINGS:
            if ceiling.startswith("task") and not has_task:
                continue
            limit = self._limits.limit(ceiling)
            if limit is not None and totals[ceiling] >= limit:
                return ceiling
        return None


def _spend_json(spend: Spend) -> dict[str, JsonValue]:
    return {
        "cents": spend.cents,
        "tokens": spend.tokens,
        "unpriced_calls": spend.unpriced_calls,
    }


def _money(cents: float) -> str:
    """Cents as dollars, with enough precision to be honest about a small amount.

    Two decimals rounds every sub-cent number to `$0.01`, which showed a spend of
    0.75 cents and a limit of 0.5 cents as the same figure — found on the first live
    run. A cheap model really does cost fractions of a cent per call (the reason
    `Usage.cost_cents` is a float at all), so small amounts get four.
    """
    return f"${cents / 100:.2f}" if cents >= 10 else f"${cents / 100:.4f}"
