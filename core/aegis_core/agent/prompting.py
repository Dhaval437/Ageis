"""Versioned system prompts and model-facing tool definitions."""

from __future__ import annotations

from importlib import resources
from typing import Final

from pydantic import JsonValue

from aegis_core.models.schemas import SystemMessage, ToolDef
from aegis_core.models.tool_names import ToolNameEncodingError, tool_wire_name
from aegis_core.tools import ToolRegistry

PLANNER_PROMPT_VERSION: Final = "planner-v1"
_PROMPT_FILES: Final = {PLANNER_PROMPT_VERSION: "planner-v1.md"}


class PromptContractError(ValueError):
    """A prompt resource or model-facing tool contract is incomplete."""


def load_system_prompt(version: str = PLANNER_PROMPT_VERSION) -> SystemMessage:
    """Load one immutable prompt version from the installed package."""
    filename = _PROMPT_FILES.get(version)
    if filename is None:
        raise PromptContractError(f"Unknown system prompt version {version!r}.")
    content = (
        resources.files("aegis_core.agent.prompts")
        .joinpath(filename)
        .read_text(encoding="utf-8")
        .strip()
    )
    if not content:
        raise PromptContractError(f"System prompt {version!r} is empty.")
    return SystemMessage(content=content)


def serialize_tools(registry: ToolRegistry) -> tuple[ToolDef, ...]:
    """Turn registered tool schemas into the provider-neutral model contract."""
    definitions: list[ToolDef] = []
    for tool in registry:
        schema = registry.parameter_schema(tool.name)
        description = schema.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PromptContractError(
                f"{tool.name!r}'s parameter schema needs a model-facing description."
            )
        try:
            tool_wire_name(tool.name)
        except ToolNameEncodingError as exc:
            raise PromptContractError(f"{tool.name!r} cannot be sent to a model provider.") from exc
        parameters: dict[str, JsonValue] = {
            key: value for key, value in schema.items() if key not in {"title", "description"}
        }
        definitions.append(
            ToolDef(
                name=tool.name,
                description=description.strip(),
                parameters=parameters,
            )
        )
    return tuple(definitions)
