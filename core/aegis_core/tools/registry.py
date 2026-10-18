"""The fail-closed registry for tools offered to the agent.

Registration proves the static contract once. `validate()` is the boundary between a
model-produced argument object and the Guardian: only the JSON-normalised output of the
tool's Pydantic schema may continue.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Final, cast, get_args

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from aegis_core.guardian.policy import GuardedTool
from aegis_core.server.schemas import RiskTier

_TOOL_NAME: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+").fullmatch
_RISK_TIERS: Final = frozenset(get_args(RiskTier))
_JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


class ToolRegistryError(Exception):
    """Base class for errors at the tool registry boundary."""


class ToolRegistrationError(ToolRegistryError):
    """A tool does not satisfy the registry contract."""


class UnknownToolError(ToolRegistryError):
    """A model requested a tool that was not registered."""


class ToolArgumentsError(ToolRegistryError):
    """A model's arguments failed the registered tool's schema."""

    def __init__(self, name: str, error_count: int) -> None:
        suffix = "" if error_count == 1 else "s"
        super().__init__(f"{name!r} received {error_count} invalid argument{suffix}.")
        self.name = name
        self.error_count = error_count


class ToolRegistry:
    """Registered tools, keyed by their stable dotted names."""

    def __init__(self) -> None:
        self._tools: dict[str, GuardedTool] = {}

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[GuardedTool]:
        return iter(self._tools.values())

    def register(self, candidate: object) -> None:
        """Register one complete tool, refusing ambiguity instead of guessing."""
        name = getattr(candidate, "name", None)
        if not isinstance(name, str) or _TOOL_NAME(name) is None:
            raise ToolRegistrationError(
                "A tool name must be a dotted lowercase identifier such as 'fs.read_file'."
            )
        if name in self._tools:
            raise ToolRegistrationError(f"A tool named {name!r} is already registered.")

        schema = getattr(candidate, "schema", None)
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise ToolRegistrationError(f"{name!r} must declare a Pydantic model as its schema.")
        if schema.model_config.get("extra") != "forbid":
            raise ToolRegistrationError(
                f"{name!r}'s schema must set extra='forbid' so unknown arguments are rejected."
            )
        try:
            _JSON_OBJECT.validate_python(schema.model_json_schema())
        except Exception as exc:
            raise ToolRegistrationError(f"{name!r}'s schema is not valid JSON Schema.") from exc

        risk = getattr(candidate, "risk", None)
        if risk not in _RISK_TIERS:
            raise ToolRegistrationError(f"{name!r} must declare a valid risk tier.")
        if not callable(getattr(candidate, "describe", None)):
            raise ToolRegistrationError(f"{name!r} must define describe(params).")
        if not callable(getattr(candidate, "targets", None)):
            raise ToolRegistrationError(f"{name!r} must define targets(params).")

        undo = getattr(candidate, "undo", None)
        undoable = getattr(candidate, "undoable", None)
        if callable(undo):
            if undoable is False:
                raise ToolRegistrationError(
                    f"{name!r} defines undo() but also declares undoable = False."
                )
        elif undoable is not False:
            raise ToolRegistrationError(
                f"{name!r} must define undo() or explicitly declare undoable = False."
            )

        self._tools[name] = cast(GuardedTool, candidate)

    def get(self, name: str) -> GuardedTool:
        """Return a registered tool, or reject an invented name."""
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"No tool named {name!r} is registered.") from None

    def validate(
        self, name: str, params: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Validate and JSON-normalise model-produced parameters."""
        tool = self.get(name)
        try:
            validated = tool.schema.model_validate(dict(params))
        except ValidationError as exc:
            raise ToolArgumentsError(name, exc.error_count()) from None
        return _JSON_OBJECT.validate_python(
            validated.model_dump(mode="json", by_alias=True)
        )

    def parameter_schema(self, name: str) -> dict[str, JsonValue]:
        """Return the JSON Schema providers will receive for one tool."""
        return _JSON_OBJECT.validate_python(self.get(name).schema.model_json_schema())

    def is_reversible(self, name: str) -> bool:
        """Whether the approval UI may truthfully call this tool recoverable."""
        return callable(getattr(self.get(name), "undo", None))
