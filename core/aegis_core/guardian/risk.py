"""The tier model (`ARCHITECTURE.md § 8.1`, `§ 10`): what a risk tier means under each
autonomy level, and what "escalate one tier" does.

Pure and total: every (tier, autonomy) pair has an answer here and there is no other
place one is decided. The two rules nothing can change are enforced in this table
rather than hoped for downstream:

- **`DANGEROUS` never runs unattended** (invariant 4). No autonomy level maps it to
  `allow`, and escalation can only move a call *towards* it.
- **`FORBIDDEN` is always `deny`.** Escalation never produces it: `FORBIDDEN` means
  "matched the compiled-in list" (`rules.py`), not "looked suspicious".

`observe` executes nothing (`§ 10`: "every action is a preview"), so it allows only
`SAFE` — observation and reading, without which the agent cannot plan at all.
"""

from __future__ import annotations

from typing import Final

from aegis_core.server.schemas import Autonomy, Decision, RiskTier

#: Least to most dangerous. `escalate()` walks this, and `higher()` compares on it.
TIER_ORDER: Final[tuple[RiskTier, ...]] = ("SAFE", "CAUTION", "DANGEROUS", "FORBIDDEN")

#: The highest tier escalation can reach. `FORBIDDEN` is a list, not a level of worry.
ESCALATION_CEILING: Final[RiskTier] = "DANGEROUS"

#: `ARCHITECTURE.md § 10`, written out cell by cell so a change is a visible diff.
#: `trusted` differs from `standard` only in that user always-allow rules apply
#: (`policy.py`), which is not a property of the tier.
_TABLE: Final[dict[Autonomy, dict[RiskTier, Decision]]] = {
    "observe": {"SAFE": "allow", "CAUTION": "deny", "DANGEROUS": "deny", "FORBIDDEN": "deny"},
    "guided": {"SAFE": "allow", "CAUTION": "confirm", "DANGEROUS": "confirm", "FORBIDDEN": "deny"},
    "standard": {"SAFE": "allow", "CAUTION": "allow", "DANGEROUS": "confirm", "FORBIDDEN": "deny"},
    "trusted": {"SAFE": "allow", "CAUTION": "allow", "DANGEROUS": "confirm", "FORBIDDEN": "deny"},
}


def decide(tier: RiskTier, autonomy: Autonomy) -> Decision:
    """The decision a call of `tier` gets under `autonomy`, before any rule or signal."""
    return _TABLE[autonomy][tier]


def escalate(tier: RiskTier) -> RiskTier:
    """One tier up, stopping at `ESCALATION_CEILING`; `FORBIDDEN` stays `FORBIDDEN`."""
    if tier == "FORBIDDEN":
        return tier
    index = min(TIER_ORDER.index(tier) + 1, TIER_ORDER.index(ESCALATION_CEILING))
    return TIER_ORDER[index]


def higher(a: RiskTier, b: RiskTier) -> RiskTier:
    """The more dangerous of two tiers."""
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def stricter(a: Decision, b: Decision) -> Decision:
    """The more restrictive of two decisions: `deny` > `confirm` > `allow`."""
    order: tuple[Decision, ...] = ("allow", "confirm", "deny")
    return a if order.index(a) >= order.index(b) else b
