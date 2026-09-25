"""Approvals and always-allow rules (`P3-11`): the broker, the rules, the policy step."""

from __future__ import annotations

import asyncio
import itertools
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

import pytest
from aegis_core.guardian.allow_rules import (
    AllowRule,
    RuleError,
    eligible,
    folder_for,
    params_digest,
    rule_fields,
)
from aegis_core.guardian.approvals import (
    TIMEOUT_S,
    ApprovalBroker,
    ApprovalError,
    ApprovalNotFoundError,
    Resolution,
)
from aegis_core.guardian.policy import Guardian, Signal, TaskContext, Verdict
from aegis_core.guardian.rules import Target, canonical_path, parse_rules
from aegis_core.server.schemas import Autonomy, EventType, RiskTier
from aegis_core.storage.allow_rules import AllowRuleStore, StoredRule
from aegis_core.storage.db import StorageError, connect, migrate
from pydantic import JsonValue

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


@dataclass(frozen=True)
class FakeTool:
    name: str = "fs.read_file"
    risk: RiskTier = "SAFE"
    touches: tuple[Target, ...] = ()

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        return self.touches


@dataclass
class Events:
    seen: list[tuple[EventType, dict[str, JsonValue], str | None]] = field(default_factory=list)

    def __call__(
        self, kind: EventType, payload: Mapping[str, JsonValue], task: str | None
    ) -> object:
        self.seen.append((kind, dict(payload), task))
        return None

    def of(self, kind: EventType) -> list[dict[str, JsonValue]]:
        return [payload for k, payload, _task in self.seen if k == kind]


def confirm(tier: RiskTier = "SAFE", *signals: Signal, stage: str = "scope") -> Verdict:
    return Verdict("confirm", tier, stage, "Asked.", None, frozenset(signals))  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AllowRuleStore]:
    conn = connect(tmp_path / "db" / "aegis.db", cross_thread=True)
    migrate(conn)
    with AllowRuleStore(conn) as opened:
        yield opened


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    place = tmp_path / "Manuals"
    place.mkdir()
    return place


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def ask(
    broker: ApprovalBroker, verdict: Verdict, tool: FakeTool, **kw: object
) -> tuple[asyncio.Task[Resolution], int]:
    """Start a request, give it a turn to publish, and return its task and id."""
    task = asyncio.create_task(broker.request(tool, {"path": "x"}, verdict, prompt="Read it", **kw))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    return task, broker.pending[-1]


# --------------------------------------------------------------------------- #
# The broker
# --------------------------------------------------------------------------- #


def test_a_request_is_published_and_the_answer_returned() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events)
        task, approval_id = await ask(broker, confirm(), FakeTool(), task_id="7")
        [requested] = events.of("approval.requested")
        assert requested["approval_id"] == approval_id
        assert requested["prompt"] == "Read it"
        assert requested["timeout_s"] == TIMEOUT_S["SAFE"]
        resolution = broker.resolve(approval_id, "allow")
        assert (await task) == resolution
        assert resolution.allowed and resolution.decided_by == "user"
        assert broker.pending == ()

    run(scenario())
    [resolved] = events.of("approval.resolved")
    assert resolved["choice"] == "allow"
    assert events.seen[-1][2] == "7"


def test_nobody_answering_is_a_deny() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events, timeouts={**TIMEOUT_S, "SAFE": 0.05})
        resolution = await broker.request(FakeTool(), {}, confirm(), prompt="p")
        assert (resolution.choice, resolution.decided_by) == ("deny", "timeout")
        assert not resolution.allowed

    run(scenario())
    assert events.of("approval.resolved")[0]["decided_by"] == "timeout"


def test_the_timeouts_are_30s_for_dangerous_and_60s_otherwise() -> None:
    assert TIMEOUT_S["DANGEROUS"] == 30.0
    assert TIMEOUT_S["CAUTION"] == TIMEOUT_S["SAFE"] == 60.0


def test_an_approval_can_be_answered_once_and_never_after_its_timeout() -> None:
    async def scenario() -> None:
        broker = ApprovalBroker(Events(), timeouts={**TIMEOUT_S, "SAFE": 0.05})
        task, approval_id = await ask(broker, confirm(), FakeTool())
        broker.resolve(approval_id, "deny")
        with pytest.raises(ApprovalNotFoundError):
            broker.resolve(approval_id, "allow")
        await task
        late = await broker.request(FakeTool(), {}, confirm(), prompt="p")
        with pytest.raises(ApprovalNotFoundError):
            broker.resolve(late.approval_id, "allow")
        with pytest.raises(ApprovalNotFoundError):
            broker.resolve(999, "allow")

    run(scenario())


def test_a_stop_denies_everything_pending() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events)
        first, _ = await ask(broker, confirm(), FakeTool())
        second, _ = await ask(broker, confirm("CAUTION"), FakeTool())
        assert broker.deny_all() == 2
        for task in (first, second):
            resolution = await task
            assert (resolution.choice, resolution.decided_by) == ("deny", "stopped")

    run(scenario())


def test_a_cancelled_wait_closes_its_approval_as_denied() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events)
        task, approval_id = await ask(broker, confirm(), FakeTool())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker.pending == ()
        with pytest.raises(ApprovalNotFoundError):
            broker.resolve(approval_id, "allow")

    run(scenario())
    assert events.of("approval.resolved")[0]["decided_by"] == "stopped"


def test_only_a_confirm_is_asked_about() -> None:
    async def scenario() -> None:
        broker = ApprovalBroker(Events())
        allow = Verdict("allow", "SAFE", "tier", "Fine.")
        with pytest.raises(ValueError, match="confirm"):
            await broker.request(FakeTool(), {}, allow, prompt="p")

    run(scenario())


def test_a_long_prompt_is_clipped() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events)
        task = asyncio.create_task(broker.request(FakeTool(), {}, confirm(), prompt="x" * 10_000))
        await asyncio.sleep(0)
        broker.deny_all()
        await task

    run(scenario())
    assert len(str(events.of("approval.requested")[0]["prompt"])) <= 4_000


# --------------------------------------------------------------------------- #
# Allow always
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "offered"),
    [
        (confirm("SAFE"), True),
        (confirm("CAUTION", stage="tier"), True),
        (confirm("DANGEROUS", stage="tier"), False),
        (confirm("CAUTION", "echoes_observation", stage="tier"), False),
        (confirm("CAUTION", "novel_target", stage="tier"), True),
    ],
    ids=["safe", "caution", "dangerous", "echo", "novel"],
)
def test_allow_always_is_offered_only_where_a_rule_could_apply(
    store: AllowRuleStore, verdict: Verdict, offered: bool
) -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events, store)
        task, approval_id = await ask(broker, verdict, FakeTool())
        if offered:
            broker.resolve(approval_id, "allow_always", "exact")
        else:
            with pytest.raises(ApprovalError, match="Allow it once"):
                broker.resolve(approval_id, "allow_always", "exact")
            broker.resolve(approval_id, "deny")
        await task

    run(scenario())
    assert events.of("approval.requested")[0]["allow_always"] is offered
    assert bool(store.list()) is offered


def test_without_a_rule_store_allow_always_is_never_offered() -> None:
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events)
        task, approval_id = await ask(broker, confirm(), FakeTool())
        with pytest.raises(ApprovalError):
            broker.resolve(approval_id, "allow_always", "exact")
        broker.resolve(approval_id, "allow")
        await task

    run(scenario())
    assert events.of("approval.requested")[0]["allow_always"] is False


def test_allow_always_needs_a_kind_and_nothing_else_takes_one(store: AllowRuleStore) -> None:
    async def scenario() -> None:
        broker = ApprovalBroker(Events(), store)
        task, approval_id = await ask(broker, confirm(), FakeTool())
        with pytest.raises(ApprovalError, match="Choose what"):
            broker.resolve(approval_id, "allow_always")
        with pytest.raises(ApprovalError, match="Only Allow always"):
            broker.resolve(approval_id, "allow", "exact")
        broker.resolve(approval_id, "deny")
        await task

    run(scenario())


def test_the_rule_is_made_from_the_pending_call(store: AllowRuleStore, docs: Path) -> None:
    tool = FakeTool(touches=(Target("path", str(docs / "a.pdf"), "read"),))
    events = Events()

    async def scenario() -> None:
        broker = ApprovalBroker(events, store)
        task = asyncio.create_task(
            broker.request(tool, {"path": "a.pdf"}, confirm(), prompt="p", task_id="t1")
        )
        await asyncio.sleep(0)
        resolution = broker.resolve(broker.pending[-1], "allow_always", "tool_in_folder")
        assert (await task).rule == resolution.rule

    run(scenario())
    [requested] = events.of("approval.requested")
    assert requested["folder"] == canonical_path(str(docs))
    assert requested["rule_kinds"] == ["exact", "tool_in_folder", "tool_for_task"]
    [stored] = store.list()
    assert (stored.kind, stored.tool, stored.folder) == (
        "tool_in_folder",
        "fs.read_file",
        canonical_path(str(docs)),
    )


def test_a_task_rule_needs_a_task(store: AllowRuleStore) -> None:
    async def scenario() -> None:
        broker = ApprovalBroker(Events(), store)
        task, approval_id = await ask(broker, confirm(), FakeTool())
        with pytest.raises(ApprovalError, match="task"):
            broker.resolve(approval_id, "allow_always", "tool_for_task")
        broker.resolve(approval_id, "deny")
        await task

    run(scenario())


# --------------------------------------------------------------------------- #
# What rules match
# --------------------------------------------------------------------------- #


def test_the_params_digest_ignores_key_order() -> None:
    assert params_digest({"a": 1, "b": [1, 2]}) == params_digest({"b": [1, 2], "a": 1})
    assert params_digest({"a": 1}) != params_digest({"a": 2})


def test_an_exact_rule_matches_only_the_same_params() -> None:
    rule = AllowRule(1, "exact", "fs.read_file", params_digest=params_digest({"path": "x"}))
    assert rule.matches("fs.read_file", {"path": "x"}, (), None)
    assert not rule.matches("fs.read_file", {"path": "y"}, (), None)
    assert not rule.matches("fs.write_file", {"path": "x"}, (), None)


def test_a_folder_rule_needs_every_path_inside_it(docs: Path, tmp_path: Path) -> None:
    folder = canonical_path(str(docs))
    rule = AllowRule(1, "tool_in_folder", "fs.read_file", folder=folder)
    inside = Target("path", str(docs / "sub" / "a.pdf"), "read")
    outside = Target("path", str(tmp_path / "b.pdf"), "read")
    sibling = Target("path", str(docs) + "Old\\c.pdf", "read")
    assert rule.matches("fs.read_file", {}, (inside,), None)
    assert not rule.matches("fs.read_file", {}, (inside, outside), None)
    assert not rule.matches("fs.read_file", {}, (sibling,), None)
    assert not rule.matches("fs.read_file", {}, (), None)
    assert not rule.matches("fs.read_file", {}, (inside, Target("command", "x")), None)


def test_a_folder_rule_never_covers_a_command_or_key_that_names_a_path_in_it(docs: Path) -> None:
    # The text of a command or a registry key is not a path, even when it looks like one.
    folder = canonical_path(str(docs))
    assert folder is not None
    rule = AllowRule(1, "tool_in_folder", "shell.run", folder=folder)
    for target in (
        Target("command", folder + "\\evil.exe --wipe"),
        Target("registry", folder + "\\key"),
    ):
        assert not rule.matches("shell.run", {}, (target,), None), target


def test_a_task_rule_matches_only_its_task() -> None:
    rule = AllowRule(1, "tool_for_task", "fs.read_file", task_id="t1")
    assert rule.matches("fs.read_file", {}, (), "t1")
    assert not rule.matches("fs.read_file", {}, (), "t2")
    assert not rule.matches("fs.read_file", {}, (), None)


def test_the_folder_offered_is_the_one_the_paths_share(docs: Path) -> None:
    one = (Target("path", str(docs / "a.pdf")),)
    two = (Target("path", str(docs / "x" / "a.pdf")), Target("path", str(docs / "y" / "b.pdf")))
    assert folder_for(one) == canonical_path(str(docs))
    assert folder_for(two) == canonical_path(str(docs))
    assert folder_for((Target("command", "dir"),)) is None
    assert folder_for((Target("path", "C:\\a.txt"),)) is None  # never a whole drive
    assert folder_for(()) is None


def test_a_folder_rule_for_a_call_with_no_folder_is_refused() -> None:
    with pytest.raises(RuleError, match="one folder"):
        rule_fields("tool_in_folder", "t", {}, (Target("command", "dir"),), None)


def test_an_unreadable_stored_row_is_not_a_rule() -> None:
    assert AllowRule.from_stored(StoredRule(1, "exact", "t", None, None, None, "x")) is None
    assert AllowRule.from_stored(StoredRule(2, "everything", "t", None, None, None, "x")) is None


def test_the_rule_table_refuses_a_rule_that_would_match_everything(store: AllowRuleStore) -> None:
    import sqlite3

    for kind in ("exact", "tool_in_folder", "tool_for_task"):
        with pytest.raises(StorageError, match="CHECK constraint"):
            store.add(kind, "fs.read_file")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO allow_rules (kind, tool, created_at) VALUES ('any', 't', 'x')"
        )


def test_rules_are_listed_newest_first_and_revocable(store: AllowRuleStore) -> None:
    first = store.add("tool_for_task", "a", task_id="t")
    second = store.add("exact", "b", params_digest="0" * 64)
    assert [r.id for r in store.list()] == [second.id, first.id]
    assert store.delete(first.id)
    assert not store.delete(first.id)
    assert [r.id for r in store.list()] == [second.id]


# --------------------------------------------------------------------------- #
# The policy step: what a matching rule may relax
# --------------------------------------------------------------------------- #

AUTONOMIES: tuple[Autonomy, ...] = get_args(Autonomy)
SIGNALS: tuple[Signal, ...] = get_args(Signal)


@pytest.fixture
def guardian() -> Guardian:
    return Guardian(parse_rules("version: 1\nforbidden: []\n"))


def everything_rules(tool: str, docs: Path) -> tuple[AllowRule, ...]:
    """One rule of each kind, all matching the calls below."""
    return (
        AllowRule(1, "exact", tool, params_digest=params_digest({})),
        AllowRule(2, "tool_in_folder", tool, folder=canonical_path(str(docs))),
        AllowRule(3, "tool_for_task", tool, task_id="t"),
    )


def test_a_rule_relaxes_a_read_outside_the_scope_under_trusted(
    guardian: Guardian, docs: Path
) -> None:
    tool = FakeTool(touches=(Target("path", str(docs / "a.pdf"), "read"),))
    rules = (AllowRule(9, "tool_in_folder", tool.name, folder=canonical_path(str(docs))),)
    ctx = TaskContext("trusted", allow_rules=rules, task_id="t")
    verdict = guardian.evaluate(tool, {}, ctx)
    assert (verdict.decision, verdict.stage, verdict.allow_rule) == ("allow", "rule", 9)
    # The same rule does nothing below trusted (`§ 10`).
    standard = guardian.evaluate(tool, {}, TaskContext("standard", allow_rules=rules))
    assert standard.decision == "confirm"


def test_a_rule_never_relaxes_a_deny(guardian: Guardian, docs: Path) -> None:
    tool = FakeTool(risk="CAUTION", touches=(Target("path", str(docs / "a.pdf"), "write"),))
    ctx = TaskContext("trusted", allow_rules=everything_rules(tool.name, docs), task_id="t")
    assert guardian.evaluate(tool, {}, ctx).decision == "deny"  # a write outside scope


def test_no_rule_ever_lets_a_dangerous_or_echoed_call_run(guardian: Guardian, docs: Path) -> None:
    signal_sets = [frozenset(c) for n in range(3) for c in itertools.combinations(SIGNALS, n)]
    target = Target("path", str(docs / "a.pdf"), "read")
    for risk_tier, autonomy, signals in itertools.product(
        ("SAFE", "CAUTION", "DANGEROUS"), AUTONOMIES, signal_sets
    ):
        tool = FakeTool(risk=risk_tier, touches=(target,))  # type: ignore[arg-type]
        ctx = TaskContext(
            autonomy,
            signals=signals,
            allow_rules=everything_rules(tool.name, docs),
            task_id="t",
        )
        verdict = guardian.evaluate(tool, {}, ctx)
        if verdict.tier == "DANGEROUS" or "echoes_observation" in signals:
            assert verdict.decision != "allow", (risk_tier, autonomy, signals)
        if verdict.stage == "rule":
            assert autonomy == "trusted" and eligible(
                Verdict("confirm", verdict.tier, "tier", "", None, signals)
            )


def test_a_rule_for_another_tool_does_not_apply(guardian: Guardian, docs: Path) -> None:
    tool = FakeTool(touches=(Target("path", str(docs / "a.pdf"), "read"),))
    ctx = TaskContext("trusted", allow_rules=everything_rules("other.tool", docs), task_id="t")
    assert guardian.evaluate(tool, {}, ctx).decision == "confirm"
