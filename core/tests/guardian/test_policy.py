"""Tests for `guardian/policy.py` — `Guardian.evaluate()` (`ARCHITECTURE.md § 8.1`)."""

from __future__ import annotations

import dataclasses
import itertools
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import pytest
from aegis_core.guardian import risk
from aegis_core.guardian.policy import (
    MAX_TARGETS,
    Guardian,
    Signal,
    TaskContext,
    Verdict,
)
from aegis_core.guardian.rules import Target, canonical_path, parse_rules
from aegis_core.server.schemas import Autonomy, RiskTier
from pydantic import JsonValue

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

TIERS: tuple[RiskTier, ...] = get_args(RiskTier)
AUTONOMIES: tuple[Autonomy, ...] = get_args(Autonomy)
SIGNALS: tuple[Signal, ...] = get_args(Signal)


@dataclass(frozen=True)
class FakeTool:
    """A tool that declares whatever targets the test gives it."""

    name: str = "fs.write_file"
    risk: RiskTier = "CAUTION"
    touches: tuple[Target, ...] = field(default_factory=tuple)

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        return self.touches


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    place = tmp_path / "Vault"
    place.mkdir()
    return place


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    place = tmp_path / "Work"
    place.mkdir()
    return place


@pytest.fixture
def guardian(vault: Path) -> Guardian:
    return Guardian(
        parse_rules(
            f"""
version: 1
forbidden:
  - id: the-vault
    kind: path
    patterns: ['{vault}']
    reason: Aegis never touches the vault.
  - id: no-format
    kind: command
    patterns: ['\\bformat\\b']
    reason: Aegis never formats a drive.
"""
        )
    )


def scope_of(*roots: Path) -> TaskContext:
    """A context whose scope is `roots`, prefix-checked on canonical paths."""
    bases = [canonical_path(str(root)) for root in roots]

    def in_scope(path: str) -> bool:
        return any(b is not None and (path == b or path.startswith(b + "\\")) for b in bases)

    return TaskContext(autonomy="standard", in_scope=in_scope)


def with_(ctx: TaskContext, **changes: Any) -> TaskContext:
    return dataclasses.replace(ctx, **changes)


# --------------------------------------------------------------------------- #
# 1. FORBIDDEN, and what cannot be checked
# --------------------------------------------------------------------------- #


def test_a_forbidden_target_is_denied_with_the_rules_own_reason(
    guardian: Guardian, vault: Path
) -> None:
    secret = str(vault / "keys.txt")
    tool = FakeTool(touches=(Target("path", secret, "read"),))
    verdict = guardian.evaluate(tool, {}, scope_of(vault))
    assert verdict.decision == "deny"
    assert verdict.stage == "forbidden"
    assert verdict.tier == "FORBIDDEN"
    assert verdict.rule_id == "the-vault"
    assert verdict.reason == "Aegis never touches the vault."
    assert "keys" not in verdict.reason


def test_forbidden_wins_over_a_scope_that_contains_everything(
    guardian: Guardian, vault: Path
) -> None:
    ctx = TaskContext(autonomy="trusted", in_scope=lambda _path: True)
    tool = FakeTool(risk="SAFE", touches=(Target("path", str(vault), "read"),))
    assert guardian.evaluate(tool, {}, ctx).decision == "deny"


def test_one_forbidden_target_among_harmless_ones_denies_the_call(
    guardian: Guardian, vault: Path, workspace: Path
) -> None:
    tool = FakeTool(
        touches=(
            Target("path", str(workspace / "a.txt")),
            Target("path", str(vault / "b.txt")),
        )
    )
    assert guardian.evaluate(tool, {}, scope_of(workspace)).stage == "forbidden"


def test_a_forbidden_command_is_denied(guardian: Guardian) -> None:
    tool = FakeTool(name="shell.run_powershell", risk="DANGEROUS")
    tool = FakeTool(tool.name, tool.risk, (Target("command", "Format D: /q"),))
    assert guardian.evaluate(tool, {}, TaskContext("trusted")).rule_id == "no-format"


@pytest.mark.parametrize("autonomy", AUTONOMIES)
def test_a_tool_declared_forbidden_never_runs(guardian: Guardian, autonomy: Autonomy) -> None:
    verdict = guardian.evaluate(FakeTool(risk="FORBIDDEN"), {}, TaskContext(autonomy))
    assert (verdict.decision, verdict.stage) == ("deny", "forbidden")


@pytest.mark.parametrize(
    "target",
    [
        Target("path", "relative\\file.txt"),
        Target("path", ""),
        Target("path", "C:\\bad\x00name"),
        Target("registry", "NotAHive\\Run"),
        Target("command", "x" * 70_000),
    ],
    ids=["relative", "empty", "nul", "no-hive", "over-long"],
)
def test_a_target_that_cannot_be_checked_is_denied(guardian: Guardian, target: Target) -> None:
    verdict = guardian.evaluate(
        FakeTool(risk="SAFE", touches=(target,)), {}, TaskContext("trusted")
    )
    assert (verdict.decision, verdict.stage) == ("deny", "unchecked")


def test_too_many_targets_are_denied_without_checking_each(
    guardian: Guardian, workspace: Path
) -> None:
    many = tuple(Target("path", str(workspace / f"{i}.txt")) for i in range(MAX_TARGETS + 1))
    verdict = guardian.evaluate(FakeTool(touches=many), {}, scope_of(workspace))
    assert (verdict.decision, verdict.stage) == ("deny", "unchecked")


def test_exactly_the_maximum_is_still_checked(guardian: Guardian, workspace: Path) -> None:
    many = tuple(Target("path", str(workspace / f"{i}.txt")) for i in range(MAX_TARGETS))
    assert guardian.evaluate(FakeTool(touches=many), {}, scope_of(workspace)).decision == "allow"


# --------------------------------------------------------------------------- #
# 2. Scope
# --------------------------------------------------------------------------- #


def test_writing_outside_the_scope_is_denied(
    guardian: Guardian, tmp_path: Path, workspace: Path
) -> None:
    tool = FakeTool(touches=(Target("path", str(tmp_path / "Elsewhere" / "x.txt"), "write"),))
    verdict = guardian.evaluate(tool, {}, scope_of(workspace))
    assert (verdict.decision, verdict.stage) == ("deny", "scope")


def test_reading_outside_the_scope_is_asked_about(
    guardian: Guardian, tmp_path: Path, workspace: Path
) -> None:
    tool = FakeTool(risk="SAFE", touches=(Target("path", str(tmp_path / "x.txt"), "read"),))
    verdict = guardian.evaluate(tool, {}, scope_of(workspace))
    assert (verdict.decision, verdict.stage) == ("confirm", "scope")


def test_inside_the_scope_the_tier_decides(guardian: Guardian, workspace: Path) -> None:
    tool = FakeTool(touches=(Target("path", str(workspace / "sub" / "x.txt"), "write"),))
    verdict = guardian.evaluate(tool, {}, scope_of(workspace))
    assert (verdict.decision, verdict.stage) == ("allow", "tier")


def test_the_scope_is_checked_on_the_canonical_path(guardian: Guardian, workspace: Path) -> None:
    escaping = str(workspace) + "\\..\\Elsewhere\\x.txt"
    tool = FakeTool(touches=(Target("path", escaping, "write"),))
    assert guardian.evaluate(tool, {}, scope_of(workspace)).stage == "scope"


def test_the_default_scope_is_empty(guardian: Guardian, workspace: Path) -> None:
    # Until P3-10 supplies one, nothing on disk is in scope: fail closed.
    tool = FakeTool(touches=(Target("path", str(workspace / "x.txt"), "write"),))
    assert guardian.evaluate(tool, {}, TaskContext("trusted")).stage == "scope"


def test_scope_only_applies_to_paths(guardian: Guardian) -> None:
    tool = FakeTool(risk="CAUTION", touches=(Target("registry", "HKCU\\Software\\Aegis"),))
    assert guardian.evaluate(tool, {}, TaskContext("standard")).decision == "allow"


def test_a_scope_read_does_not_soften_a_deny(guardian: Guardian, tmp_path: Path) -> None:
    tool = FakeTool(risk="CAUTION", touches=(Target("path", str(tmp_path / "x"), "read"),))
    verdict = guardian.evaluate(tool, {}, TaskContext("observe"))
    assert verdict.decision == "deny"


# --------------------------------------------------------------------------- #
# 3. Tier, autonomy and signals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tier", "autonomy"), list(itertools.product(("SAFE", "CAUTION", "DANGEROUS"), AUTONOMIES))
)
def test_with_nothing_else_the_table_decides(
    guardian: Guardian, tier: RiskTier, autonomy: Autonomy
) -> None:
    verdict = guardian.evaluate(FakeTool(risk=tier), {}, TaskContext(autonomy))
    assert verdict.decision == risk.decide(tier, autonomy)
    assert (verdict.tier, verdict.stage) == (tier, "tier")


@pytest.mark.parametrize("signal", SIGNALS)
def test_any_signal_raises_the_tier_one_step(guardian: Guardian, signal: Signal) -> None:
    verdict = guardian.evaluate(
        FakeTool(risk="CAUTION"), {}, TaskContext("standard", signals=frozenset({signal}))
    )
    assert verdict.tier == "DANGEROUS"
    assert verdict.decision == "confirm"
    assert verdict.signals == frozenset({signal})


def test_signals_raise_one_step_however_many_there_are(guardian: Guardian) -> None:
    ctx = TaskContext("standard", signals=frozenset(SIGNALS))
    assert guardian.evaluate(FakeTool(risk="SAFE"), {}, ctx).tier == "CAUTION"


def test_text_echoed_from_the_screen_is_always_asked_about(guardian: Guardian) -> None:
    # § 8.6: a SAFE call escalates only to CAUTION, which `standard` would allow.
    ctx = TaskContext("trusted", signals=frozenset({"echoes_observation"}))
    verdict = guardian.evaluate(FakeTool(risk="SAFE"), {}, ctx)
    assert verdict.decision == "confirm"
    assert "screen" in verdict.reason


def test_an_echo_does_not_turn_a_deny_into_a_question(guardian: Guardian) -> None:
    ctx = TaskContext("observe", signals=frozenset({"echoes_observation"}))
    assert guardian.evaluate(FakeTool(risk="CAUTION"), {}, ctx).decision == "deny"


# --------------------------------------------------------------------------- #
# The invariants, over every combination
# --------------------------------------------------------------------------- #

SIGNAL_SETS = [
    frozenset(c) for n in range(len(SIGNALS) + 1) for c in itertools.combinations(SIGNALS, n)
]


@dataclass(frozen=True)
class Case:
    tool: FakeTool
    ctx: TaskContext
    verdict: Verdict
    #: The same call with no signals, for the monotonicity check.
    plain: Verdict


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> list[Case]:
    """Every tier x autonomy x signal set x scope shape, evaluated once for the module."""
    root = tmp_path_factory.mktemp("sweep")
    workspace, vault = root / "Work", root / "Vault"
    workspace.mkdir()
    vault.mkdir()
    judge = Guardian(
        parse_rules(
            f"version: 1\nforbidden:\n  - id: v\n    kind: path\n"
            f"    patterns: ['{vault}']\n    reason: No.\n"
        )
    )
    inside = Target("path", str(workspace / "x.txt"), "write")
    outside_read = Target("path", str(root / "y.txt"), "read")
    forbidden = Target("path", str(vault / "z.txt"), "read")
    shapes = [(), (inside,), (outside_read,), (inside, outside_read), (inside, forbidden)]
    cases = []
    for tier, autonomy, signals, touches in itertools.product(
        TIERS, AUTONOMIES, SIGNAL_SETS, shapes
    ):
        ctx = with_(scope_of(workspace), autonomy=autonomy, signals=signals)
        tool = FakeTool(risk=tier, touches=touches)
        plain = judge.evaluate(tool, {}, with_(ctx, signals=frozenset()))
        cases.append(Case(tool, ctx, judge.evaluate(tool, {}, ctx), plain))
    return cases


def test_dangerous_is_never_allowed_in_any_combination(sweep: list[Case]) -> None:
    for case in sweep:
        if case.verdict.tier in ("DANGEROUS", "FORBIDDEN") or case.tool.risk in (
            "DANGEROUS",
            "FORBIDDEN",
        ):
            assert case.verdict.decision != "allow", case


def test_observe_never_allows_anything_but_observation(sweep: list[Case]) -> None:
    for case in sweep:
        if case.ctx.autonomy == "observe" and case.verdict.decision == "allow":
            assert case.verdict.tier == "SAFE", case


def test_signals_never_make_a_call_easier(sweep: list[Case]) -> None:
    for case in sweep:
        assert risk.stricter(case.verdict.decision, case.plain.decision) == case.verdict.decision


def test_a_forbidden_target_is_denied_in_every_combination(sweep: list[Case]) -> None:
    for case in sweep:
        if any("vault" in t.value.lower() for t in case.tool.touches):
            assert (case.verdict.decision, case.verdict.stage) == ("deny", "forbidden"), case


def test_every_verdict_has_a_reason(sweep: list[Case]) -> None:
    assert len(sweep) == len(TIERS) * len(AUTONOMIES) * len(SIGNAL_SETS) * 5
    for case in sweep:
        assert case.verdict.reason.endswith("."), case


def test_no_reason_ever_quotes_a_target(guardian: Guardian, vault: Path, tmp_path: Path) -> None:
    marker = "ZZmarkerZZ"
    for target in (
        Target("path", str(vault / marker)),
        Target("path", str(tmp_path / marker), "write"),
        Target("path", str(tmp_path / marker), "read"),
        Target("path", marker),
        Target("command", f"format {marker}"),
    ):
        verdict = guardian.evaluate(FakeTool(touches=(target,)), {}, TaskContext("guided"))
        assert marker.lower() not in verdict.reason.lower(), verdict
