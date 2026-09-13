"""HTTP and WebSocket routes, all mounted under `/v1` (`ARCHITECTURE.md § 9`).

Every route here is behind `SessionAuthMiddleware` (bearer token, no `Origin`,
peer-PID check) — see `server/auth.py`. That includes the stream: an unauthenticated
WebSocket upgrade is refused before this module ever sees it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Final

import anyio
from anyio.abc import TaskGroup
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from aegis_core import __version__
from aegis_core.server.hub import (
    CLOSE_INVALID_SINCE,
    CLOSE_REPLAY_UNAVAILABLE,
    CLOSE_UNSUPPORTED_DATA,
    Closed,
    EventHub,
    ReplayUnavailableError,
    Subscription,
)
from aegis_core.server.schemas import HealthResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

#: A `since` longer than this is not a sequence number this core could have issued.
_SINCE_PATTERN: Final = re.compile(r"[0-9]{1,15}")


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness for MAIN's supervisor: answered without touching disk or a model."""
    started: float = request.app.state.started_monotonic
    return HealthResponse(
        status="ok",
        version=__version__,
        uptime=round(time.monotonic() - started, 3),
    )


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """`WS /v1/stream?since=<seq>` — the live event bus (`ARCHITECTURE.md § 9.2`).

    Each message is one `StreamEvent` as a JSON text frame, in `seq` order. The
    socket is accepted before any refusal so the client sees a close *code* rather
    than a bare HTTP 403; the codes are documented in `server/hub.py`.
    """
    hub: EventHub = websocket.app.state.hub
    await websocket.accept()

    raw_since = websocket.query_params.get("since")
    if raw_since is not None and not _SINCE_PATTERN.fullmatch(raw_since):
        await websocket.close(CLOSE_INVALID_SINCE)
        return
    since = int(raw_since) if raw_since is not None else None

    try:
        subscription = hub.subscribe(asyncio.get_running_loop(), since)
    except ReplayUnavailableError as error:
        log.info("stream.replay_unavailable", extra={"detail": str(error)})
        await websocket.close(CLOSE_REPLAY_UNAVAILABLE)
        return

    log.info("stream.connected", extra={"since": since, "last_seq": hub.last_seq})
    try:
        code = await _run_stream(websocket, subscription)
        if code is not None:
            await _close_quietly(websocket, code)
    finally:
        hub.unsubscribe(subscription)
        log.info("stream.disconnected", extra={"last_seq": hub.last_seq})


async def _run_stream(websocket: WebSocket, subscription: Subscription) -> int | None:
    """Pump events and watch the client at once; whichever finishes first ends both.

    A task group, not bare `asyncio` tasks, so a server-side cancellation reaches
    both halves and neither can outlive the connection. Returns the close code to
    send, or `None` if the client is already gone.
    """
    outcome: list[int | None] = []

    async def settle(half: Callable[[], Awaitable[int | None]], group: TaskGroup) -> None:
        try:
            result = await half()
        except (WebSocketDisconnect, OSError):
            result = None
        if not outcome:
            outcome.append(result)
        group.cancel_scope.cancel()

    async with anyio.create_task_group() as group:
        group.start_soon(settle, lambda: _pump(websocket, subscription), group)
        group.start_soon(settle, lambda: _await_client_message(websocket), group)
    return outcome[0] if outcome else None


async def _pump(websocket: WebSocket, subscription: Subscription) -> int | None:
    """Send events until the hub ends the stream; return the close code it chose."""
    while True:
        item = await subscription.next()
        if isinstance(item, Closed):
            return item.code
        await websocket.send_text(item.model_dump_json())


async def _await_client_message(websocket: WebSocket) -> int | None:
    """Wait for the client. `None` means it disconnected; a code means it misbehaved."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    return CLOSE_UNSUPPORTED_DATA


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code)
    except (WebSocketDisconnect, RuntimeError, OSError) as error:
        # The peer left between the decision to close and the close frame.
        log.debug("stream.close_skipped", extra={"code": code, "error": type(error).__name__})
