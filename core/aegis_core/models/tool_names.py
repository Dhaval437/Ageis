"""Translate AEGIS dotted tool names to provider-safe function names."""

from __future__ import annotations

import base64
from typing import Final

WIRE_PREFIX: Final = "aegis_"
MAX_WIRE_NAME_LENGTH: Final = 64


class ToolNameEncodingError(ValueError):
    """A tool name cannot be represented on a provider wire."""


def tool_wire_name(name: str) -> str:
    """Encode one internal dotted name using only provider-portable characters."""
    encoded = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
    wire_name = f"{WIRE_PREFIX}{encoded}"
    if len(wire_name) > MAX_WIRE_NAME_LENGTH:
        raise ToolNameEncodingError("The tool name is too long for a model provider.")
    return wire_name


def tool_internal_name(name: str) -> str:
    """Restore an encoded name; preserve legacy names for recorded conversations."""
    if not name.startswith(WIRE_PREFIX):
        return name
    encoded = name.removeprefix(WIRE_PREFIX)
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        internal = decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolNameEncodingError("The model returned an invalid tool name.") from exc
    if not internal or tool_wire_name(internal) != name:
        raise ToolNameEncodingError("The model returned an invalid tool name.")
    return internal
