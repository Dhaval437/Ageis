"""The FastAPI application factory.

The app is created, never imported as a module-level singleton, so tests get an
isolated instance with its own uptime clock.

No interactive docs and no `openapi.json`: this server exists for one client — MAIN —
and an unauthenticated schema endpoint on the user's loopback is surface we do not need.

Every app is wrapped in `SessionAuthMiddleware`. There is no unauthenticated mode and
no flag that makes one: an app object that could be served without credentials is an
app object somebody eventually serves without credentials.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aegis_core import __version__
from aegis_core.server.auth import SessionAuth, SessionAuthMiddleware
from aegis_core.server.routes import router

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("core.started", extra={"version": __version__})
    try:
        yield
    finally:
        uptime = round(time.monotonic() - app.state.started_monotonic, 3)
        log.info("core.stopped", extra={"uptime": uptime})


def create_app(auth: SessionAuth) -> FastAPI:
    """Build the core's HTTP app, authenticated against this session's token."""
    app = FastAPI(
        title="AEGIS core",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.started_monotonic = time.monotonic()
    app.include_router(router)
    # Added last so it wraps everything, including FastAPI's own 404 and 405
    # responses: an unauthenticated caller must not be able to map the routes.
    app.add_middleware(SessionAuthMiddleware, auth=auth)
    return app
