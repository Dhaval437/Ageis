"""HTTP routes, all mounted under `/v1` (`ARCHITECTURE.md § 9.1`).

Only `/health` exists so far. Bearer auth and the peer-PID check arrive with the
handshake in P0-06; until then the app binds loopback and nothing else.
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
