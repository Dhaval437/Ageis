"""Pydantic models for the core's HTTP surface.

`ARCHITECTURE.md § 4`: these models are the single source of truth for the TypeScript
types in `packages/shared` — the generator lands in P0-11. Never hand-write a TS type
that mirrors one of them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """`GET /v1/health` (`ARCHITECTURE.md § 9.1`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = Field(description="Always `ok`; a core that cannot answer is down.")
    version: str = Field(description="The `aegis_core` package version.")
    uptime: float = Field(ge=0, description="Seconds since the app was created.")
