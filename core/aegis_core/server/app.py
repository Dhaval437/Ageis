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

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aegis_core import __version__
from aegis_core.actuation.killswitch import KillSwitch
from aegis_core.guardian.approvals import ApprovalBroker
from aegis_core.models.service import ModelService
from aegis_core.server.auth import SessionAuth, SessionAuthMiddleware
from aegis_core.server.hub import EventHub
from aegis_core.server.routes import router

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("core.started", extra={"version": __version__})
    try:
        yield
    finally:
        # First: a shutting-down core must not leave a question it can no longer act on.
        app.state.approvals.close()
        app.state.hub.close()
        models: ModelService | None = app.state.models
        if models is not None:
            await models.aclose()
        uptime = round(time.monotonic() - app.state.started_monotonic, 3)
        log.info("core.stopped", extra={"uptime": uptime})


async def _on_invalid_request(request: Request, exc: Exception) -> JSONResponse:
    """Answer a malformed request without repeating any of it back.

    FastAPI's own handler returns the offending *input* alongside each error. The one
    client here is a renderer that displays text the agent scraped off the user's screen
    and is the field a key is typed into (`PUT /models/keys/{id}`), so an echo is a way
    for either to end up in a log, a devtools panel or a bug report. The renderer is
    already the hostile caller of `ARCHITECTURE.md § 9.3`; a malformed request from it is
    a bug in it, and the *location* it names is enough to find one.
    """
    log.warning("request.invalid", extra={"path": request.url.path, "field": _first_loc(exc)})
    return JSONResponse(status_code=422, content={"detail": "Aegis could not read that request."})


def _first_loc(exc: Exception) -> str:
    """Where the first problem was, as a dotted path. Names only, never values."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover — handler is registered
        return ""  # for this one type
    first = next(iter(exc.errors()), None)
    return "" if first is None else ".".join(str(part) for part in first.get("loc", ()))


def create_app(
    auth: SessionAuth,
    hub: EventHub | None = None,
    models: ModelService | None = None,
    kill_switch: KillSwitch | None = None,
    approvals: ApprovalBroker | None = None,
) -> FastAPI:
    """Build the core's HTTP app, authenticated against this session's token.

    `hub` is the event bus behind `WS /v1/stream`; publishers reach it as
    `app.state.hub`. One is created if none is given.

    `models` is the Models screen's service (`P1-10`), reached as `app.state.models`. It
    is optional because it owns two database connections and the Windows vault, which a
    test of the routing surface has no business opening — and because a subsystem that is
    not there is the `unavailable` every other one already answers (`P0-04`). Its routes
    answer `503` without it.

    `kill_switch` is the one every `InputController` in this process must be attached
    to, reached as `app.state.kill_switch`; `POST /v1/kill` engages it (`P3-06`). One
    is created if none is given.

    `approvals` is the broker the agent asks and `POST /v1/approvals/{id}` answers
    (`P3-11`), reached as `app.state.approvals`. Without one, a broker is made on this
    app's hub with no rule store, so *Allow always* is not offered and `/rules` is empty.
    """
    app = FastAPI(
        title="AEGIS core",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.started_monotonic = time.monotonic()
    app.state.hub = hub if hub is not None else EventHub()
    app.state.models = models
    app.state.kill_switch = kill_switch if kill_switch is not None else KillSwitch()
    hub_ = app.state.hub
    app.state.approvals = (
        approvals
        if approvals is not None
        else ApprovalBroker(lambda kind, payload, task: hub_.publish(kind, payload, task_id=task))
    )
    app.add_exception_handler(RequestValidationError, _on_invalid_request)
    app.include_router(router)
    # Added last so it wraps everything, including FastAPI's own 404 and 405
    # responses: an unauthenticated caller must not be able to map the routes.
    app.add_middleware(SessionAuthMiddleware, auth=auth)
    return app
