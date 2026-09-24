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
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from aegis_core import __version__
from aegis_core.actuation.killswitch import KillSwitch
from aegis_core.models.schemas import ProviderId
from aegis_core.models.service import ModelService
from aegis_core.server.hub import (
    CLOSE_INVALID_SINCE,
    CLOSE_REPLAY_UNAVAILABLE,
    CLOSE_UNSUPPORTED_DATA,
    Closed,
    EventHub,
    ReplayUnavailableError,
    Subscription,
)
from aegis_core.server.schemas import (
    HealthResponse,
    KeyRequest,
    KeyResponse,
    KillResponse,
    ModelCatalog,
    SettingsRequest,
    SettingsResponse,
    SpendResponse,
    ValidateRequest,
    ValidateResponse,
)
from aegis_core.storage.db import StorageError
from aegis_core.storage.vault import VaultError

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


@router.post("/kill", response_model=KillResponse)
async def kill(request: Request) -> KillResponse:
    """The kill switch's polite half (`P3-06`): freeze, and release every held key.

    Deliberately `async` and synchronous inside: it runs on the event loop rather than
    queueing for a worker thread, so a saturated thread pool cannot delay it — and if
    the loop itself is wedged, MAIN's deadline passes and it terminates the process,
    which is the answer a wedged core should get.
    """
    switch: KillSwitch = request.app.state.kill_switch
    report = switch.engage()
    return KillResponse(
        engaged=True,
        released_keys=report.released_keys,
        released_buttons=report.released_buttons,
        release_failures=report.release_failures,
    )


# ---------------------------------------------------------------------------
# The Models screen (`ARCHITECTURE.md § 9.1`, `UI.md § 8.4`)
# ---------------------------------------------------------------------------
#
# Every one of these is a thin shell over `models/service.py`. Three rules hold across
# all of them, and they are why the shell exists at all:
#
# * **A key travels in and never out.** `PUT /models/keys/{id}` is the only route that
#   accepts one, and nothing answers with one — `has_key` and `mask_key()`'s `sk-…abcd`
#   are the whole vocabulary (`ARCHITECTURE.md § 5.3`).
# * **A refusal the user can act on is a 400 with the service's own message**, which is
#   written for them and quotes neither a key nor an address (`P1-05`, `P1-07`).
# * **A subsystem that is not there answers 503**, not a 500. The core can be served
#   without a `ModelService` — every test in `tests/server` does — and a screen told
#   *unavailable* can say so.


def _service(request: Request) -> ModelService:
    """The app's `ModelService`, or a 503 the renderer can render."""
    service: ModelService | None = getattr(request.app.state, "models", None)
    if service is None:
        raise HTTPException(status_code=503, detail="The model layer is not available.")
    return service


@router.get("/models/catalog", response_model=ModelCatalog)
async def models_catalog(request: Request) -> ModelCatalog:
    """Every provider, its models, and whether a key is saved for it."""
    return await _service(request).catalog()


@router.post("/models/validate", response_model=ValidateResponse)
async def models_validate(request: Request, body: ValidateRequest) -> ValidateResponse:
    """The *Test* button: one real call to the provider, timed. Never 500s for a bad key."""
    return await _service(request).validate(body.provider_id)


@router.put("/models/keys/{provider_id}", response_model=KeyResponse)
async def models_set_key(
    request: Request, provider_id: ProviderId, body: KeyRequest
) -> KeyResponse:
    """Save the key for one provider.

    The body is the only place in the whole surface a key appears, and it is never
    echoed: the answer is the masked form. Every refusal comes from `KeyVault`, whose
    messages say what was wrong without quoting any part of what was sent.
    """
    service = _service(request)
    try:
        masked = service.set_key(provider_id, body.key)
    except VaultError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return KeyResponse(provider_id=provider_id, has_key=masked is not None, masked_key=masked)


@router.delete("/models/keys/{provider_id}", response_model=KeyResponse)
async def models_delete_key(request: Request, provider_id: ProviderId) -> KeyResponse:
    """Remove the key for one provider. Removing one that is not there is not an error."""
    service = _service(request)
    try:
        service.delete_key(provider_id)
    except VaultError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return KeyResponse(provider_id=provider_id, has_key=False, masked_key=None)


@router.get("/models/spend", response_model=SpendResponse)
async def models_spend(request: Request) -> SpendResponse:
    """Today's total and the ceilings. A first value for the meter, not a poll."""
    return _service(request).spend()


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """Everything the user has configured. Today that is the model layer and nothing else."""
    return SettingsResponse(models=_service(request).settings())


@router.put("/settings", response_model=SettingsResponse)
async def put_settings(request: Request, body: SettingsRequest) -> SettingsResponse:
    """Replace the settings document, whole. Answers with what was actually stored.

    A role chain that cannot be routed, or an address a key may not be sent to, is a 400
    naming the problem — refused here rather than at the first task, which is the whole
    reason the screen exists.
    """
    service = _service(request)
    try:
        stored = await service.save_settings(body.models)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=_first_message(error)) from error
    except StorageError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return SettingsResponse(models=stored)


def _first_message(error: ValueError) -> str:
    """One sentence out of a `ValidationError`, which is a list of them.

    Pydantic's own string is a multi-line report with a documentation URL in it, written
    for whoever wrote the request — and the request came from a screen, so what reaches
    the user has to be the sentence the validator raised.
    """
    errors = getattr(error, "errors", None)
    if callable(errors):
        messages = [str(item.get("msg", "")).removeprefix("Value error, ") for item in errors()]
        first = next((message for message in messages if message), "")
        if first:
            return first
    return str(error).splitlines()[0]


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
