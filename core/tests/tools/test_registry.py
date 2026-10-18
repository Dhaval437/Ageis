"""The tool registry rejects incomplete contracts and unvalidated arguments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aegis_core.guardian.rules import Target
from aegis_core.server.schemas import RiskTier
from aegis_core.tools import (
    ToolArgumentsError,
    ToolRegistrationError,
    ToolRegistry,
    UnknownToolError,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1)
    when: datetime | None = None


class IrreversibleTool:
    name = "test.count"
    schema = Params
    risk: RiskTier = "CAUTION"
    undoable = False

    def describe(self, params: Mapping[str, JsonValue]) -> str:
        return f"Count to {params['count']}"

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        return ()


class ReversibleTool(IrreversibleTool):
    name = "test.reversible"
    undoable = True

    def undo(self, payload: Mapping[str, JsonValue]) -> None:
        del payload


def complete_contract(**changes: Any) -> SimpleNamespace:
    values: dict[str, object] = {
        "name": "test.complete",
        "schema": Params,
        "risk": "SAFE",
        "undoable": False,
        "describe": lambda _params: "Test action",
        "targets": lambda _params: (),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_registers_complete_tools_and_preserves_registration_order() -> None:
    first = IrreversibleTool()
    second = ReversibleTool()
    registry = ToolRegistry()

    registry.register(first)
    registry.register(second)

    assert len(registry) == 2
    assert list(registry) == [first, second]
    assert registry.get(first.name) is first
    assert registry.is_reversible(first.name) is False
    assert registry.is_reversible(second.name) is True


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("name", "name"),
        ("schema", "schema"),
        ("risk", "risk tier"),
        ("describe", "describe"),
        ("targets", "targets"),
        ("undoable", "undo"),
    ],
)
def test_every_contract_member_is_required(missing: str, message: str) -> None:
    candidate = complete_contract()
    delattr(candidate, missing)

    with pytest.raises(ToolRegistrationError, match=message):
        ToolRegistry().register(candidate)


@pytest.mark.parametrize("name", ["", "click", "Input.Click", "input.click-now"])
def test_tool_names_are_stable_dotted_identifiers(name: str) -> None:
    with pytest.raises(ToolRegistrationError, match="dotted lowercase"):
        ToolRegistry().register(complete_contract(name=name))


def test_schema_must_be_a_strict_pydantic_model() -> None:
    class PermissiveParams(BaseModel):
        count: int

    with pytest.raises(ToolRegistrationError, match="Pydantic"):
        ToolRegistry().register(complete_contract(schema=dict))
    with pytest.raises(ToolRegistrationError, match="extra='forbid'"):
        ToolRegistry().register(complete_contract(schema=PermissiveParams))


def test_invalid_risk_and_duplicate_name_are_rejected() -> None:
    registry = ToolRegistry()
    registry.register(IrreversibleTool())

    with pytest.raises(ToolRegistrationError, match="risk tier"):
        ToolRegistry().register(complete_contract(risk="LOW"))
    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(IrreversibleTool())


def test_reversibility_requires_one_unambiguous_declaration() -> None:
    contradictory = complete_contract(undo=lambda _payload: None)
    with pytest.raises(ToolRegistrationError, match="also declares"):
        ToolRegistry().register(contradictory)

    asserted_without_undo = complete_contract(undoable=True)
    with pytest.raises(ToolRegistrationError, match="define undo"):
        ToolRegistry().register(asserted_without_undo)

    real_undo = complete_contract(undoable=True, undo=lambda _payload: None)
    registry = ToolRegistry()
    registry.register(real_undo)
    assert registry.is_reversible(real_undo.name) is True


def test_model_arguments_are_validated_and_json_normalised() -> None:
    registry = ToolRegistry()
    registry.register(IrreversibleTool())

    params = registry.validate(
        "test.count", {"count": "2", "when": "2026-10-13T12:30:00Z"}
    )

    assert params == {"count": 2, "when": "2026-10-13T12:30:00Z"}
    schema = registry.parameter_schema("test.count")
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"count": 0},
        {"count": 1, "injected": "ignored by permissive schemas"},
    ],
)
def test_invalid_model_arguments_fail_closed_without_echoing_values(
    params: dict[str, JsonValue],
) -> None:
    registry = ToolRegistry()
    registry.register(IrreversibleTool())

    with pytest.raises(ToolArgumentsError) as caught:
        registry.validate("test.count", params)

    assert caught.value.error_count >= 1
    assert "ignored by permissive schemas" not in str(caught.value)


def test_unknown_tool_names_are_rejected() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError, match="registered"):
        registry.get("test.invented")
    with pytest.raises(UnknownToolError, match="registered"):
        registry.validate("test.invented", {})
