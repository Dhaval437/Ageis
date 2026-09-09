"""HTTP routes, all mounted under `/v1` (`ARCHITECTURE.md § 9.1`).

Only `/health` exists so far. Every route here is behind `SessionAuthMiddleware`
(bearer token, no `Origin`, peer-PID check) — see `server/auth.py`.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from aegis_core import __version__
from aegis_core.server.schemas import HealthResponse

router = APIRouter(prefix="/v1")


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness for MAIN's supervisor: answered without touching disk or a model."""
    started: float = request.app.state.started_monotonic
    return HealthResponse(
        status="ok",
        version=__version__,
        uptime=round(time.monotonic() - started, 3),
    )
