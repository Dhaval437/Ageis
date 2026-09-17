"""What every HTTP adapter needs, so the second one copies nothing from the first.

`P1-02` wrote all of this inside `openai.py`, where it was the only caller. `P1-03` is
the second caller, and an SSE reader duplicated into a second module is an SSE reader
that gets fixed in one of them: the reason it does not use `httpx2.EventSource`
(`REMEMBER.md § 5`, 2026-09-17) is a subtle one about the preemption path, and a copy
would lose it the first time somebody tidied it.

`REVIEW.md § 2` also wants one place where every external call's timeout lives. This is
that place. Nothing here names a provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Final, cast

import httpx2

from aegis_core.models.provider import (
    ProviderAuthError,
    ProviderCapabilityError,
    ProviderError,
    ProviderProtocolError,
    ProviderTransientError,
)
from aegis_core.models.schemas import Capabilities, ProviderId, Usage

#: How long to wait for the connection itself, however long the whole call may take.
CONNECT_TIMEOUT_S: Final = 10.0

#: A cap on one unterminated event, so a gateway cannot stream one endless line.
MAX_EVENT_BYTES: Final = 1024 * 1024

#: Nothing here may grow without a bound (`REVIEW.md § 2`). A model that streams tool
#: arguments forever is a broken model, not a big request.
MAX_TOOL_CALLS: Final = 32
MAX_TOOL_ARGUMENT_CHARS: Final = 256 * 1024

EVENT_STREAM: Final = "text/event-stream"

#: Status codes that mean something specific. Anything else is read from its class.
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR: Final = 500

#: Reads the key for this provider out of the vault. Called once per request and never
#: stored, so nothing that outlives a call holds a key (`ARCHITECTURE.md § 5.3`).
KeyLookup = Callable[[], str | None]


def new_client(base_url: str, transport: httpx2.AsyncBaseTransport | None) -> httpx2.AsyncClient:
    """One client per provider instance. It holds no key — credentials are per request."""
    return httpx2.AsyncClient(
        base_url=base_url,
        transport=transport,
        # A redirect would send the credential header somewhere the user never chose,
        # which is the whole point of the egress rule in `REMEMBER.md § 3`.
        follow_redirects=False,
        timeout=httpx2.Timeout(CONNECT_TIMEOUT_S),
    )


def media_type(response: httpx2.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


def status_error(provider_id: ProviderId, status: int) -> ProviderError:
    """Map a status to an error type. The body is never read, let alone quoted."""
    if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        return ProviderAuthError(provider_id, "that API key was rejected", status=status)
    if status == HTTP_NOT_FOUND:
        return ProviderCapabilityError(
            provider_id, "that model is not available to this key", status=status
        )
    if status == HTTP_TOO_MANY_REQUESTS:
        return ProviderTransientError(provider_id, "too many requests", status=status)
    if status >= HTTP_SERVER_ERROR:
        return ProviderTransientError(provider_id, "the provider had a server error", status=status)
    return ProviderProtocolError(provider_id, "the provider refused the request", status=status)


def cost_cents(caps: Capabilities, usage: Usage) -> float | None:
    """What a call cost, in cents. `None` when either price is unknown — never free."""
    if caps.cost_per_mtok_input is None or caps.cost_per_mtok_output is None:
        return None
    dollars = (
        usage.input_tokens * caps.cost_per_mtok_input
        + usage.output_tokens * caps.cost_per_mtok_output
    ) / 1_000_000
    return dollars * 100


async def sse_data(response: httpx2.Response, provider_id: ProviderId) -> AsyncGenerator[str, None]:
    """Yield the `data:` payload of each server-sent event.

    httpx2 ships an `EventSource`, and this does not use it: it reads the response in a
    generator of its own that a caller cannot reach, so abandoning a stream mid-flight
    — which is exactly what preemption does — leaves that generator to be finalised
    after the socket has gone, and the event loop logs an error every time. Reading the
    response here means every generator in the chain is closed, in order, on the stop
    path. The part of the format a provider actually uses is this small.

    The `event:` line is deliberately dropped. Both adapters dispatch on a `type` inside
    the payload — OpenAI has one shape, and every Anthropic event names itself — so the
    two copies of the truth cannot disagree.
    """
    # `aiter_text` is an async generator function, so what it returns really does have
    # `aclose()`; only its annotation is the wider `AsyncIterator`.
    reader = cast("AsyncGenerator[str, None]", response.aiter_text())
    buffer = ""
    data: list[str] = []
    try:
        async for chunk in reader:
            buffer += chunk
            if len(buffer) > MAX_EVENT_BYTES:
                raise ProviderProtocolError(provider_id, "an event was too large to read")
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                event = _feed(data, line)
                if event:
                    yield event
        # The stream ended without its last blank line. Read what is left rather than
        # dropping it: usage is the last thing a provider sends, and a genuinely
        # truncated tail then fails loudly on the JSON instead of costing nothing.
        if buffer:
            _feed(data, buffer)
        if data:
            yield "\n".join(data)
    finally:
        await reader.aclose()


def _feed(data: list[str], line: str) -> str | None:
    """Consume one SSE line, returning an event's payload if that line ended one."""
    line = line.rstrip("\r")
    if not line:
        if not data:
            return None
        event = "\n".join(data)
        data.clear()
        return event
    if line.startswith(":"):  # a keep-alive comment
        return None
    name, _, value = line.partition(":")
    if name == "data":
        data.append(value[1:] if value.startswith(" ") else value)
    return None


def json_object(data: str, provider_id: ProviderId) -> dict[str, object]:
    """Parse one event payload, insisting it is an object."""
    try:
        parsed = json.loads(data)
    except ValueError as exc:
        raise ProviderProtocolError(provider_id, "the stream was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderProtocolError(provider_id, "a stream chunk was not an object")
    return cast("dict[str, object]", parsed)
