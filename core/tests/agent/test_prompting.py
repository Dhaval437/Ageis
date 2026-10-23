"""The planner prompt and tool contracts are stable across provider dialects."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import pytest
from aegis_core.agent.prompting import (
    PLANNER_PROMPT_VERSION,
    PromptContractError,
    load_system_prompt,
    serialize_tools,
)
from aegis_core.guardian.rules import Target
from aegis_core.models.providers import anthropic, google, openai
from aegis_core.models.schemas import ChatRequest, TextPart, UserMessage
from aegis_core.models.tool_names import tool_internal_name, tool_wire_name
from aegis_core.server.schemas import RiskTier
from aegis_core.tools import ToolRegistry
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class CountParams(BaseModel):
    """Count to a positive number at the user's request."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1, description="The positive number to count to.")


class UndocumentedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class FakeTool:
    name = "test.count"
    schema: type[BaseModel] = CountParams
    risk: RiskTier = "SAFE"
    undoable = False

    def describe(self, params: Mapping[str, JsonValue]) -> str:
        return f"Count to {params['count']}"

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        del params
        return ()


def registry_with(tool: object) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def test_the_versioned_system_prompt_states_the_safety_contract() -> None:
    prompt = load_system_prompt()

    assert PLANNER_PROMPT_VERSION == "planner-v1"
    assert "at most one tool call" in prompt.content
    assert "untrusted data" in prompt.content
    assert "Never work around the Guardian" in prompt.content
    assert "later observation verifies" in prompt.content


def test_an_unknown_prompt_version_is_refused() -> None:
    with pytest.raises(PromptContractError, match="Unknown system prompt version"):
        load_system_prompt("planner-v2")


def test_registered_schemas_become_model_tool_definitions() -> None:
    definitions = serialize_tools(registry_with(FakeTool()))

    assert len(definitions) == 1
    assert definitions[0].name == "test.count"
    assert definitions[0].description == "Count to a positive number at the user's request."
    assert definitions[0].parameters == {
        "additionalProperties": False,
        "properties": {
            "count": {
                "description": "The positive number to count to.",
                "minimum": 1,
                "title": "Count",
                "type": "integer",
            }
        },
        "required": ["count"],
        "type": "object",
    }


def test_a_tool_without_a_model_description_is_refused() -> None:
    tool = FakeTool()
    tool.schema = UndocumentedParams

    with pytest.raises(PromptContractError, match="model-facing description"):
        serialize_tools(registry_with(tool))


def test_dotted_names_round_trip_through_a_portable_wire_name() -> None:
    wire_name = tool_wire_name("fs.list_dir")

    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", wire_name)
    assert "." not in wire_name
    assert tool_internal_name(wire_name) == "fs.list_dir"


def test_each_provider_receives_its_native_tool_schema_shape() -> None:
    definition = serialize_tools(registry_with(FakeTool()))[0]
    request = ChatRequest(
        model="model",
        messages=(UserMessage(content=(TextPart(text="count"),)),),
        tools=(definition,),
    )
    wire_name = tool_wire_name(definition.name)

    openai_body = openai.wire_body(request, openai.OPENAI)
    anthropic_body = anthropic.wire_body(request)
    google_body = google.wire_body(request)

    assert openai_body["tools"][0]["function"] == {
        "name": wire_name,
        "description": definition.description,
        "parameters": definition.parameters,
    }
    assert anthropic_body["tools"][0] == {
        "name": wire_name,
        "description": definition.description,
        "input_schema": definition.parameters,
    }
    assert google_body["tools"][0]["functionDeclarations"][0] == {
        "name": wire_name,
        "description": definition.description,
        "parameters": definition.parameters,
    }
