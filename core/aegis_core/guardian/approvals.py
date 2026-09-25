"""Approvals (`P3-11`): asking the person, waiting, and denying when nobody answers.

A tool call the Guardian answered `confirm` goes through `ApprovalBroker.request()`:
the broker publishes `approval.requested` (the renderer shows `UI.md § 5`'s dialog),
waits, and returns a `Resolution`. The person answers through `POST /v1/approvals/{id}`,
which is `resolve()`.

The rules this module exists to hold:

- **Timeouts deny** (invariant 6). The clock runs *here*, in the core — not in the
  dialog, which may never have been drawn — at 30 s for a `DANGEROUS` call and 60 s
  for anything else, and it ends in `deny`. Nothing allows because nobody answered.
- **A stop denies everything pending.** The kill switch and a stopped task call
  `deny_all()`, so no dialog left on screen can approve an action for an agent that
  is no longer supposed to act.
- **One answer per approval.** A second `resolve()`, or one after the timeout, is
  refused: the first answer is the one the agent acted on.
- **`allow_always` is offered only where it could ever apply** (`allow_rules.eligible`)
  and makes the rule from the *pending call* — its tool, its params, its folder, its
  task — never from anything in the request, so the renderer cannot name a path.

The broker runs on the core's event loop; `request()` is awaited there and `resolve()`
is called from a route on the same loop.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from aegis_core.guardian.allow_rules import (
    AllowRule,
    RuleError,
    eligible,
    folder_for,
    rule_fields,
)
from aegis_core.guardian.policy import GuardedTool, Verdict
from aegis_core.guardian.rules import Target
from aegis_core.server.schemas import (
    ApprovalChoice,
    ApprovalOutcomeSource,
    EventType,
    RiskTier,
    RuleKind,
)
from aegis_core.storage.allow_rules import AllowRuleStore
from aegis_core.storage.db import StorageError

#: `UI.md § 5`: how long an approval waits before it denies itself.
TIMEOUT_S: Final[Mapping[RiskTier, float]] = {
    "SAFE": 60.0,
    "CAUTION": 60.0,
    "DANGEROUS": 30.0,
    "FORBIDDEN": 0.0,
}

#: The longest a prompt or reason may be in an event. `describe()` is the tool's own
#: sentence, but a path in it can be long; the dialog's "show all" is `P3-12`'s.
MAX_TEXT: Final = 4_000

Publish = Callable[[EventType, Mapping[str, JsonValue], "str | None"], object]


class ApprovalError(Exception):
    """A `resolve()` that cannot be accepted. The message is a sentence for the user."""


class ApprovalNotFoundError(ApprovalError):
    """No pending approval has that id: it was answered, timed out, or never existed."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """How an approval ended. `P4` records it in `approvals` and `steps.approved_by`."""

    approval_id: int
    choice: ApprovalChoice
    decided_by: ApprovalOutcomeSource
    rule: AllowRule | None = None

    @property
    def allowed(self) -> bool:
        return self.choice != "deny"


@dataclass(slots=True)
class _Pending:
    tool: str
    params: Mapping[str, JsonValue]
    targets: tuple[Target, ...]
    task_id: str | None
    offer_rule: bool
    future: asyncio.Future[Resolution] = field(repr=False)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


class ApprovalBroker:
    """Pending approvals, their deadlines, and the rules *Allow always* makes."""

    def __init__(
        self,
        publish: Publish,
        rules: AllowRuleStore | None = None,
        *,
        timeouts: Mapping[RiskTier, float] = TIMEOUT_S,
    ) -> None:
        self._publish = publish
        self._rules = rules
        self._timeouts = timeouts
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}

    def close(self) -> None:
        """Deny whatever is pending and release the rule store. At shutdown."""
        self.deny_all("stopped")
        if self._rules is not None:
            self._rules.close()

    @property
    def pending(self) -> tuple[int, ...]:
        return tuple(self._pending)

    @property
    def rule_store(self) -> AllowRuleStore | None:
        """Where rules are kept, for the Rules screen's list and revoke. `None` if nowhere."""
        return self._rules

    def rules(self) -> tuple[AllowRule, ...]:
        """Every readable always-allow rule, for `TaskContext.allow_rules`."""
        if self._rules is None:
            return ()
        loaded = (AllowRule.from_stored(stored) for stored in self._rules.list())
        return tuple(rule for rule in loaded if rule is not None)

    async def request(
        self,
        tool: GuardedTool,
        params: Mapping[str, JsonValue],
        verdict: Verdict,
        *,
        prompt: str,
        why: str | None = None,
        task_id: str | None = None,
        reversible: bool | None = None,
    ) -> Resolution:
        """Ask the person about a `confirm`, and wait for them — or for the deadline.

        `reversible` is whether the tool has a real `undo()` (`P4-01`). Anything but
        `True` makes the dialog say the action cannot be undone: the banner may only say
        "recoverable" when it is (`UI.md § 5`, `RECOVERY.md` principle 2).
        """
        if verdict.decision != "confirm":
            raise ValueError("Only a `confirm` verdict is asked about.")
        approval_id = next(self._ids)
        targets = tuple(tool.targets(params))
        offer_rule = self._rules is not None and eligible(verdict)
        future: asyncio.Future[Resolution] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = _Pending(
            tool.name, params, targets, task_id, offer_rule, future
        )
        timeout = self._timeouts[verdict.tier]
        folder = folder_for(targets) if offer_rule else None
        self._publish(
            "approval.requested",
            {
                "approval_id": approval_id,
                "tool": tool.name,
                "prompt": _clip(prompt),
                "why": None if why is None else _clip(why),
                "tier": verdict.tier,
                "reason": verdict.reason,
                "timeout_s": timeout,
                "allow_always": offer_rule,
                "rule_kinds": self._kinds(offer_rule, folder, task_id),
                "folder": folder,
                "reversible": reversible is True,
            },
            task_id,
        )
        try:
            resolution = await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            resolution = self._finish(approval_id, "deny", "timeout")
        except asyncio.CancelledError:
            # The task waiting on it was stopped: close the approval as denied, so the
            # dialog is dismissed and a late click has nothing to approve.
            self._finish(approval_id, "deny", "stopped")
            raise
        finally:
            self._pending.pop(approval_id, None)
        return resolution

    @staticmethod
    def _kinds(offer: bool, folder: str | None, task_id: str | None) -> list[JsonValue]:
        if not offer:
            return []
        kinds: list[JsonValue] = ["exact"]
        if folder is not None:
            kinds.append("tool_in_folder")
        if task_id is not None:
            kinds.append("tool_for_task")
        return kinds

    def resolve(
        self, approval_id: int, choice: ApprovalChoice, rule: RuleKind | None = None
    ) -> Resolution:
        """The person's answer. Raises `ApprovalError` with a sentence if it is refused."""
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            raise ApprovalNotFoundError(
                "That approval was already answered, or it timed out and was denied."
            )
        if choice != "allow_always":
            if rule is not None:
                raise ApprovalError("Only Allow always creates a rule.")
            return self._finish(approval_id, choice, "user")
        if rule is None:
            raise ApprovalError("Choose what Allow always should cover.")
        if not pending.offer_rule or self._rules is None:
            raise ApprovalError("This action cannot be always allowed. Allow it once instead.")
        try:
            fields = rule_fields(
                rule, pending.tool, pending.params, pending.targets, pending.task_id
            )
            stored = self._rules.add(rule, pending.tool, **fields)
        except RuleError as error:
            raise ApprovalError(str(error)) from error
        except StorageError as error:
            raise ApprovalError("The rule could not be saved. Allow it once instead.") from error
        return self._finish(approval_id, "allow_always", "user", AllowRule.from_stored(stored))

    def deny_all(self, decided_by: ApprovalOutcomeSource = "stopped") -> int:
        """Deny every pending approval. Returns how many there were."""
        denied = 0
        for approval_id, pending in list(self._pending.items()):
            if not pending.future.done():
                self._finish(approval_id, "deny", decided_by)
                denied += 1
        return denied

    def _finish(
        self,
        approval_id: int,
        choice: ApprovalChoice,
        decided_by: ApprovalOutcomeSource,
        rule: AllowRule | None = None,
    ) -> Resolution:
        resolution = Resolution(approval_id, choice, decided_by, rule)
        pending = self._pending.get(approval_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(resolution)
        self._publish(
            "approval.resolved",
            {
                "approval_id": approval_id,
                "choice": choice,
                "decided_by": decided_by,
                "rule_id": None if rule is None else rule.id,
            },
            None if pending is None else pending.task_id,
        )
        return resolution
