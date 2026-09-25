"""`Guardian.evaluate()`: the policy check every tool call passes through (`§ 8.1`).

The Guardian sits downstream of the model and trusts nothing it produced. It sees a
tool (its name and declared risk tier), the targets the call would touch — declared by
the *tool* from the validated params, never by the model — and the task's context, and
answers `allow`, `confirm` or `deny` with a reason a person can read.

The order, first match wins for a `deny`:

1. **FORBIDDEN** — a target under a `rules.yaml` entry, or a tool declared FORBIDDEN, or
   a target that cannot be put in canonical form (it cannot be checked, so it cannot
   pass). Hard `deny`; the reason is the rule's own sentence and never quotes the target.
2. **Scope** (`§ 8.2`) — a path outside the task's scope: writing is `deny`, reading is
   `confirm`. The scope itself is `P3-10`'s; until then a context has an empty scope,
   which is the fail-closed reading.
3. **Tier and autonomy** (`risk.py`), after **anomaly signals** raise the tier one step.
   A call that echoes text from an observation is at least `confirm` whatever its tier
   (`§ 8.6`), because that is the shape of a prompt injection.

`§ 8.1`'s sketch has user always-allow rules as a step between scope and tier. They are
not here: under every autonomy level the only tier `confirm` left for them to relax is
`DANGEROUS`, which invariant 4 says they may not, so what they *should* relax (a scope
read, an escalation of a `CAUTION` tool) is the design of `P3-11`'s `allow_always` —
and it must then run after the signals, never before, or an "always allow" would wave
a prompt-injected call through.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from aegis_core.guardian import risk
from aegis_core.guardian.rules import Rules, Target, canonical_target
from aegis_core.server.schemas import Autonomy, Decision, RiskTier

#: More than any honest call touches. A call claiming more is not checked target by
#: target; it is refused.
MAX_TARGETS: Final = 64

Signal = Literal["echoes_observation", "high_rate", "novel_target", "sensitive_content"]
"""An anomaly the loop noticed about this call (`§ 8.1` step 5, `§ 8.6`). Any one of them
raises the tier by one; `echoes_observation` also floors the decision at `confirm`."""

Stage = Literal["forbidden", "unchecked", "scope", "tier"]
"""Which step decided: the UI offers different things for a scope `deny` (widen the
scope) than for a FORBIDDEN one (nothing)."""


class GuardedTool(Protocol):
    """What the Guardian needs of a tool. `P4-01`'s registry holds tools to it."""

    @property
    def name(self) -> str: ...

    @property
    def risk(self) -> RiskTier: ...

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        """Every path, command or registry key the call would touch, from its params."""
        ...


def _empty_scope(_path: str) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What the Guardian knows about the task a call belongs to."""

    autonomy: Autonomy
    #: Whether a **canonical** path is inside the task's scope. `P3-10` supplies it;
    #: the default is an empty scope, so nothing on disk is in it.
    in_scope: Callable[[str], bool] = _empty_scope
    signals: frozenset[Signal] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Verdict:
    """The Guardian's answer for one call."""

    decision: Decision
    #: The tier after escalation — what `steps.risk` records.
    tier: RiskTier
    stage: Stage
    #: One sentence for the approval dialog, the timeline and the audit log. It names
    #: the rule or the reason, never the target, which may be anything the model wrote.
    reason: str
    #: The FORBIDDEN entry that matched, if one did.
    rule_id: str | None = None
    signals: frozenset[Signal] = frozenset()


class GuardianDeniedError(Exception):
    """`check_target()` refused. The message is for the user and quotes no target."""


class Guardian:
    """The policy engine. One per core; `evaluate()` is pure given its inputs."""

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    def check_target(self, target: Target, ctx: TaskContext) -> str:
        """The re-check a tool makes **immediately before** it acts (`REVIEW.md § 5`).

        `evaluate()` ran when the call was proposed; since then an approval may have
        waited half a minute and the disk may have changed under it — a folder swapped
        for a junction is the classic. So the tool asks again about each target it is
        about to touch and acts on the canonical form this returns, never on the string
        it was given. Refused: anything FORBIDDEN, anything with no canonical form, and
        a write outside the scope. A read outside the scope is not refused here: it was
        either inside it or approved when `evaluate()` asked.

        Raises `GuardianDeniedError`, whose message is a sentence for the user.
        """
        canonical = canonical_target(target)
        if canonical is None:
            raise GuardianDeniedError(
                "Aegis could not check where this action points, so it stopped."
            )
        entry = self._rules.forbidden_match(target)
        if entry is not None:
            raise GuardianDeniedError(entry.reason)
        if target.kind == "path" and target.access == "write" and not ctx.in_scope(canonical):
            raise GuardianDeniedError("This would change something outside the task's scope.")
        return canonical

    def evaluate(
        self, tool: GuardedTool, params: Mapping[str, JsonValue], ctx: TaskContext
    ) -> Verdict:
        declared = tool.risk
        targets = tuple(tool.targets(params))

        # 1. FORBIDDEN, and anything that cannot be checked.
        if declared == "FORBIDDEN":
            return Verdict("deny", "FORBIDDEN", "forbidden", "Aegis never runs this tool.")
        if len(targets) > MAX_TARGETS:
            return Verdict(
                "deny", declared, "unchecked", "This action touches too many things to check."
            )
        canonical: list[tuple[Target, str]] = []
        for target in targets:
            form = canonical_target(target)
            if form is None:
                return Verdict(
                    "deny",
                    declared,
                    "unchecked",
                    "Aegis could not check where this action points, so it will not run it.",
                )
            canonical.append((target, form))
        for target, _form in canonical:
            entry = self._rules.forbidden_match(target)
            if entry is not None:
                return Verdict("deny", "FORBIDDEN", "forbidden", entry.reason, rule_id=entry.id)

        # 2. Scope: writing outside it is refused, reading outside it is asked about.
        scope_confirm = False
        for target, form in canonical:
            if target.kind != "path" or ctx.in_scope(form):
                continue
            if target.access == "write":
                return Verdict(
                    "deny",
                    declared,
                    "scope",
                    "This would change something outside the task's scope.",
                )
            scope_confirm = True

        # 3. Signals raise the tier; the tier and autonomy decide.
        tier = risk.escalate(declared) if ctx.signals else declared
        decision = risk.decide(tier, ctx.autonomy)
        if decision == "deny":
            return Verdict(decision, tier, "tier", _TIER_REASONS[decision], None, ctx.signals)
        if "echoes_observation" in ctx.signals:
            reason = "This action repeats text from the screen that was not in your request."
            return Verdict("confirm", tier, "tier", reason, None, ctx.signals)
        if scope_confirm:
            reason = "This would read something outside the task's scope."
            return Verdict("confirm", tier, "scope", reason, None, ctx.signals)
        return Verdict(decision, tier, "tier", _TIER_REASONS[decision], None, ctx.signals)


_TIER_REASONS: Final[Mapping[Decision, str]] = {
    "allow": "Allowed by your autonomy setting.",
    "confirm": "Your autonomy setting asks you to approve this.",
    "deny": "Your autonomy setting does not let Aegis do this.",
}
