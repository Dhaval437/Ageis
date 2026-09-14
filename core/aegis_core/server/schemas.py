"""Pydantic models for the core's HTTP and WebSocket surface.

`ARCHITECTURE.md § 4`: these models are the single source of truth for the TypeScript
types in `packages/shared/src/api.ts`, which `aegis_core.server.typegen` renders from
this module (`pnpm gen:types`). Never hand-write a TS type that mirrors one of them.

Every `BaseModel` defined here and every module-level `Literal` alias is exported. An
alias is documented by a string literal on the line after it, which becomes its JSDoc.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class HealthResponse(BaseModel):
    """`GET /v1/health` (`ARCHITECTURE.md § 9.1`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = Field(description="Always `ok`; a core that cannot answer is down.")
    version: str = Field(description="The `aegis_core` package version.")
    uptime: float = Field(ge=0, description="Seconds since the app was created.")


RiskTier = Literal["SAFE", "CAUTION", "DANGEROUS", "FORBIDDEN"]
"""Risk tiers every tool call is classified into. See `REMEMBER.md § 6`."""


EventType = Literal[
    "task.created",
    "task.status",
    "step.started",
    "step.thought",
    "step.action",
    "step.observation",
    "approval.requested",
    "approval.resolved",
    "preempt.triggered",
    "error",
    "cost.updated",
    "log",
]
"""Every event type on `WS /v1/stream`. Adding one is an `ARCHITECTURE.md § 9.2` edit first."""


class StreamEvent(BaseModel):
    """One message on `WS /v1/stream` (`ARCHITECTURE.md § 9.2`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1, description="Monotonic within one core process, starting at 1.")
    ts: str = Field(description="UTC ISO-8601 with milliseconds.")
    task_id: str | None = Field(description="The task this belongs to; `null` if app-wide.")
    type: EventType
    payload: dict[str, JsonValue] = Field(description="Type-specific body.")
