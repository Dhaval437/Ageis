"""Tests for `guardian/risk.py` — the tier model (`ARCHITECTURE.md § 8.1`, `§ 10`)."""

from __future__ import annotations

import itertools
from typing import get_args

import pytest
from aegis_core.guardian import risk
from aegis_core.server.schemas import Autonomy, Decision, RiskTier

TIERS: tuple[RiskTier, ...] = get_args(RiskTier)
AUTONOMIES: tuple[Autonomy, ...] = get_args(Autonomy)
DECISIONS: tuple[Decision, ...] = get_args(Decision)


def test_the_order_covers_every_tier_exactly_once() -> None:
    assert sorted(risk.TIER_ORDER) == sorted(TIERS)
    assert len(set(risk.TIER_ORDER)) == len(TIERS)


@pytest.mark.parametrize("autonomy", AUTONOMIES)
def test_dangerous_never_runs_unattended(autonomy: Autonomy) -> None:
    # Invariant 4: no autonomy level, not even `trusted`, allows DANGEROUS.
    assert risk.decide("DANGEROUS", autonomy) != "allow"


@pytest.mark.parametrize("autonomy", AUTONOMIES)
def test_forbidden_is_always_denied(autonomy: Autonomy) -> None:
    assert risk.decide("FORBIDDEN", autonomy) == "deny"


@pytest.mark.parametrize(
    ("autonomy", "expected"),
    [
        # § 10, row by row: SAFE, CAUTION, DANGEROUS.
        ("observe", ("allow", "deny", "deny")),
        ("guided", ("allow", "confirm", "confirm")),
        ("standard", ("allow", "allow", "confirm")),
        ("trusted", ("allow", "allow", "confirm")),
    ],
)
def test_the_table_is_section_10(autonomy: Autonomy, expected: tuple[Decision, ...]) -> None:
    assert tuple(risk.decide(t, autonomy) for t in ("SAFE", "CAUTION", "DANGEROUS")) == expected


def test_observe_runs_nothing_but_observation() -> None:
    allowed = [t for t in TIERS if risk.decide(t, "observe") == "allow"]
    assert allowed == ["SAFE"]


@pytest.mark.parametrize(
    ("tier", "up"),
    [
        ("SAFE", "CAUTION"),
        ("CAUTION", "DANGEROUS"),
        ("DANGEROUS", "DANGEROUS"),
        ("FORBIDDEN", "FORBIDDEN"),
    ],
)
def test_escalation_is_one_step_and_stops_at_dangerous(tier: RiskTier, up: RiskTier) -> None:
    assert risk.escalate(tier) == up


@pytest.mark.parametrize("tier", ["SAFE", "CAUTION", "DANGEROUS"])
def test_escalation_never_invents_forbidden(tier: RiskTier) -> None:
    assert risk.escalate(risk.escalate(risk.escalate(tier))) != "FORBIDDEN"


@pytest.mark.parametrize(("tier", "autonomy"), list(itertools.product(TIERS, AUTONOMIES)))
def test_escalating_never_makes_a_call_easier(tier: RiskTier, autonomy: Autonomy) -> None:
    before = risk.decide(tier, autonomy)
    after = risk.decide(risk.escalate(tier), autonomy)
    assert risk.stricter(before, after) == after


@pytest.mark.parametrize(("a", "b"), list(itertools.product(TIERS, TIERS)))
def test_higher_is_the_more_dangerous(a: RiskTier, b: RiskTier) -> None:
    got = risk.higher(a, b)
    assert got in (a, b)
    assert risk.TIER_ORDER.index(got) == max(risk.TIER_ORDER.index(a), risk.TIER_ORDER.index(b))


@pytest.mark.parametrize(("a", "b"), list(itertools.product(DECISIONS, DECISIONS)))
def test_stricter_prefers_deny_then_confirm(a: Decision, b: Decision) -> None:
    order = ("allow", "confirm", "deny")
    assert order.index(risk.stricter(a, b)) == max(order.index(a), order.index(b))
