"""The Anthropic Messages API adapter (`ARCHITECTURE.md § 5.1`, `P1-03`).

The second of the two real adapters. Everything it shares with `openai.py` — the SSE
reader, the status map, the cost sum, the client — comes from `common.py`; what is left
here is the part of Anthropic that is genuinely its own, and it is more than a base URL,
which is why this is a module and not another `ProviderConfig`:

- **The system prompt is a top-level field**, not a message. There is no `system` role
  in the conversation array.
- **Tool results are user messages.** A `tool_result` is a content block inside a user
  turn, so a conversation that alternates user → assistant → tool → assistant is two
  consecutive user turns on the wire, which the API refuses. Adjacent same-role turns
  are coalesced (`_coalesce`).
- **`max_tokens` is required**, so a request that does not name one gets
  `DEFAULT_MAX_TOKENS` rather than a 400.
- **The stream is a sequence of named events**, not one repeated chunk shape: text and
  tool arguments arrive as deltas against a *block index*, and usage arrives split
  across the first and last event.
- **There is no JSON mode.** `Capabilities.json_mode` is `False` for every model here,
  so the capability gate refuses such a request before the wire rather than the model
  answering prose that will not parse. Getting JSON out of Claude means giving it a
  tool, which is `P4`'s business and not an adapter's.

Two things this adapter shares with the first by rule rather than by code. It never puts
a response body in an error, because a provider's error body is the likeliest place for
a key to be echoed back (`REVIEW.md § 5`). And it never emits a partial tool call:
arguments are buffered and flushed once, at the end, so the delta order is the same for
every provider and the Guardian never sees half a call.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2

from aegis_core.models.provider import (
    ProviderAuthError,
    ProviderCapabilityError,
    ProviderProtocolError,
    ProviderTransientError,
)
from aegis_core.models.providers.common import (
    CONNECT_TIMEOUT_S,
    EVENT_STREAM,
    HTTP_FORBIDDEN,
    HTTP_UNAUTHORIZED,
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_CALLS,
    KeyLookup,
    cost_cents,
    json_object,
    media_type,
    new_client,
    sse_data,
    status_error,
)
from aegis_core.models.schemas import (
    AssistantMessage,
    Capabilities,
    ChatDelta,
    ChatRequest,
    ContentPart,
    DoneDelta,
    FinishReason,
    ImagePart,
    KeyStatus,
    ProviderId,
    SystemMessage,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallDelta,
    ToolChoice,
    ToolResultMessage,
    Usage,
    UsageDelta,
    UserMessage,
)

log = logging.getLogger(__name__)

PROVIDER: Final[ProviderId] = "anthropic"

BASE_URL: Final = "https://api.anthropic.com/v1"

#: Pinned, not `latest`. The wire format this adapter reads is the one this version
#: promises; a silently newer format is exactly the kind of break nobody notices until
#: an agent is halfway through a task.
API_VERSION: Final = "2023-06-01"

#: The Messages API requires `max_tokens`, and `ChatRequest.max_output_tokens` is
#: optional because most APIs do not. A request without one gets this rather than a 400.
DEFAULT_MAX_TOKENS: Final = 4096

#: The Messages API takes 0 to 1, while `ChatRequest` allows 0 to 2 because OpenAI does.
MAX_TEMPERATURE: Final = 1.0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _caps(
    *,
    ctx_window: int = 200_000,
    cost_in: float | None,
    cost_out: float | None,
    vision: bool = True,
    tool_calling: bool = True,
) -> Capabilities:
    return Capabilities(
        vision=vision,
        tool_calling=tool_calling,
        # No model here has one. See the module docstring.
        json_mode=False,
        ctx_window=ctx_window,
        cost_per_mtok_input=cost_in,
        cost_per_mtok_output=cost_out,
    )


#: A snapshot of Anthropic's published list prices, in **US dollars per million
#: tokens**, taken 2026-09-17. Both the alias (`claude-sonnet-4-5`) and the dated id a
#: user may paste from the console resolve to the same entry.
#:
#: A wrong price makes the `P1-09` budget guard wrong in the direction of spending more
#: than the user allowed, so this table is only what is published, and anything not in
#: it falls through to `ANTHROPIC_UNKNOWN` with **no price at all** rather than a
#: guessed one. `P1-10`'s Models screen shows the user what we think a model costs.
ANTHROPIC_MODELS: Final[Mapping[str, Capabilities]] = {
    "claude-opus-4-1": _caps(cost_in=15.00, cost_out=75.00),
    "claude-opus-4-1-20250805": _caps(cost_in=15.00, cost_out=75.00),
    "claude-opus-4-0": _caps(cost_in=15.00, cost_out=75.00),
    "claude-opus-4-20250514": _caps(cost_in=15.00, cost_out=75.00),
    "claude-sonnet-4-5": _caps(cost_in=3.00, cost_out=15.00),
    "claude-sonnet-4-5-20250929": _caps(cost_in=3.00, cost_out=15.00),
    "claude-sonnet-4-0": _caps(cost_in=3.00, cost_out=15.00),
    "claude-sonnet-4-20250514": _caps(cost_in=3.00, cost_out=15.00),
    "claude-haiku-4-5": _caps(cost_in=1.00, cost_out=5.00),
    "claude-haiku-4-5-20251001": _caps(cost_in=1.00, cost_out=5.00),
    "claude-3-7-sonnet-latest": _caps(cost_in=3.00, cost_out=15.00),
    "claude-3-5-haiku-latest": _caps(cost_in=0.80, cost_out=4.00),
}

#: What a model this build has never heard of can be assumed to do. Unlike `openai`,
#: where an unknown id is a typo and `capabilities()` raises, Anthropic ships models
#: faster than this table is updated, and every model it has shipped for years sees
#: images and calls tools. A typo still fails — as the API's own 404, one call later.
#: The **price is unknown, never free** (`Capabilities`).
ANTHROPIC_UNKNOWN: Final = _caps(cost_in=None, cost_out=None)


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------

_TOOL_CHOICE: Final[Mapping[ToolChoice, dict[str, Any]]] = {
    "auto": {"type": "auto"},
    "none": {"type": "none"},
    "required": {"type": "any"},
}


def _wire_part(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    # Unlike Chat Completions, this is a first-class block and is accepted inside a
    # tool result too, so an observation returned by a tool does not have to be dropped.
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": part.media_type, "data": part.data},
    }


def _wire_message(
    message: UserMessage | AssistantMessage | ToolResultMessage,
) -> tuple[str, list[dict[str, Any]]]:
    """One message as its wire role and content blocks.

    A `SystemMessage` is deliberately not in the signature: it has no wire role here,
    and `wire_body` lifts it into the top-level `system` field instead.
    """
    if isinstance(message, UserMessage):
        return "user", [_wire_part(part) for part in message.content]

    if isinstance(message, AssistantMessage):
        blocks = [_wire_part(part) for part in message.content]
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in message.tool_calls
        )
        return "assistant", blocks

    # A tool result is a block inside a *user* turn here, not a role of its own.
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": [_wire_part(part) for part in message.content],
    }
    if message.is_error:
        result["is_error"] = True
    return "user", [result]


def _coalesce(turns: Sequence[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Merge adjacent turns with the same role.

    The API refuses two user turns in a row, and a conversation that ends a step with a
    tool result produces exactly that — the result is a user turn, and so is the next
    thing the user says. Merging blocks is what the roles mean anyway.
    """
    merged: list[dict[str, Any]] = []
    for role, blocks in turns:
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"].extend(blocks)
        else:
            merged.append({"role": role, "content": list(blocks)})
    return merged


def wire_body(req: ChatRequest) -> dict[str, Any]:
    """Build the Messages request body. Pure, so the mapping is testable."""
    system = "\n\n".join(m.content for m in req.messages if isinstance(m, SystemMessage))
    turns = [_wire_message(m) for m in req.messages if not isinstance(m, SystemMessage)]

    body: dict[str, Any] = {
        "model": req.model,
        "messages": _coalesce(turns),
        "stream": True,
        "max_tokens": req.max_output_tokens or DEFAULT_MAX_TOKENS,
    }
    if system:
        body["system"] = system
    if req.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in req.tools
        ]
        body["tool_choice"] = _TOOL_CHOICE[req.tool_choice]
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.stop:
        body["stop_sequences"] = list(req.stop)
    return body


# ---------------------------------------------------------------------------
# Wire → deltas
# ---------------------------------------------------------------------------

_STOP_REASONS: Final[Mapping[str, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

#: The one mid-stream error type that is worth another provider's attention. Everything
#: else the API can send mid-stream is a bug in the request or in this adapter.
_TRANSIENT_ERRORS: Final = frozenset({"overloaded_error", "api_error", "rate_limit_error"})


@dataclass
class _PartialToolCall:
    id: str
    name: str
    arguments: str = ""


@dataclass
class MessageStream:
    """Reads one Messages SSE stream into deltas.

    Separate from the adapter because this is where the awkward parts of the format
    live — deltas addressed to a block index rather than to the message, usage split
    between the first event and the last — and those deserve tests that need no socket.
    """

    provider_id: ProviderId = PROVIDER
    finish_reason: FinishReason | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    saw_usage: bool = False
    _calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    _texts: set[int] = field(default_factory=set)
    _argument_chars: int = 0

    def read(self, data: str) -> list[ChatDelta]:
        """Turn one event payload into the deltas it produced, if any."""
        event = json_object(data, self.provider_id)
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ProviderProtocolError(self.provider_id, "a stream event had no type")

        if kind == "error":
            raise self._error(event.get("error"))
        if kind == "message_start":
            self._read_message_start(event.get("message"))
        elif kind == "content_block_start":
            self._read_block_start(event)
        elif kind == "content_block_delta":
            return self._read_block_delta(event)
        elif kind == "message_delta":
            self._read_message_delta(event)
        # `ping`, `content_block_stop` and `message_stop` carry nothing this needs, and
        # an event type added after `API_VERSION` was pinned must not break a live task.
        return []

    # -- events ------------------------------------------------------------

    def _error(self, raw: object) -> ProviderProtocolError | ProviderTransientError:
        """A mid-stream error event. Its `message` is the provider's prose — never read."""
        kind = raw.get("type") if isinstance(raw, dict) else None
        if isinstance(kind, str) and kind in _TRANSIENT_ERRORS:
            return ProviderTransientError(self.provider_id, "the provider was overloaded")
        return ProviderProtocolError(self.provider_id, "the provider ended the stream early")

    def _read_message_start(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            return
        # Cached tokens are billed differently and nothing asks for caching yet, but
        # they are still tokens the user was charged for, so they are counted as input.
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and value > 0:
                self.input_tokens += value
                self.saw_usage = True
        self._read_output_tokens(usage)

    def _read_block_start(self, event: Mapping[str, object]) -> None:
        index = self._index_of(event)
        block = event.get("content_block")
        if not isinstance(block, dict):
            raise ProviderProtocolError(self.provider_id, "a content block was not an object")
        kind = block.get("type")
        if kind == "text":
            self._texts.add(index)
            return
        if kind != "tool_use":
            # A `thinking` block, or whatever a later model emits. Nothing asks for one,
            # and its deltas are ignored below because the index is in neither set.
            return
        if len(self._calls) >= MAX_TOOL_CALLS:
            raise ProviderProtocolError(
                self.provider_id, "the model asked for too many tools at once"
            )
        call_id = block.get("id")
        name = block.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ProviderProtocolError(self.provider_id, "a tool call had no name or id")
        self._calls[index] = _PartialToolCall(id=call_id, name=name)

    def _read_block_delta(self, event: Mapping[str, object]) -> list[ChatDelta]:
        index = self._index_of(event)
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise ProviderProtocolError(self.provider_id, "a content delta was not an object")

        kind = delta.get("type")
        if kind == "text_delta" and index in self._texts:
            text = delta.get("text")
            if isinstance(text, str) and text:
                return [TextDelta(text=text)]
        elif kind == "input_json_delta":
            partial = self._calls.get(index)
            fragment = delta.get("partial_json")
            if partial is not None and isinstance(fragment, str) and fragment:
                self._argument_chars += len(fragment)
                if self._argument_chars > MAX_TOOL_ARGUMENT_CHARS:
                    raise ProviderProtocolError(
                        self.provider_id, "the tool arguments were too large to read"
                    )
                partial.arguments += fragment
        return []

    def _read_message_delta(self, event: Mapping[str, object]) -> None:
        delta = event.get("delta")
        if isinstance(delta, dict):
            reason = delta.get("stop_reason")
            if isinstance(reason, str):
                self.finish_reason = _STOP_REASONS.get(reason)
                if self.finish_reason is None:
                    raise ProviderProtocolError(
                        self.provider_id, f"unknown stop reason {reason[:32]!r}"
                    )
        usage = event.get("usage")
        if isinstance(usage, dict):
            self._read_output_tokens(usage)

    def _read_output_tokens(self, usage: Mapping[str, object]) -> None:
        """`output_tokens` is cumulative, so the last event to carry it wins."""
        value = usage.get("output_tokens")
        if isinstance(value, int) and value >= 0:
            self.output_tokens = value
            self.saw_usage = True

    def _index_of(self, event: Mapping[str, object]) -> int:
        index = event.get("index")
        if not isinstance(index, int):
            raise ProviderProtocolError(self.provider_id, "a content block had no index")
        return index

    # -- the end -----------------------------------------------------------

    def usage(self) -> Usage | None:
        """What the call cost in tokens, or `None` if the provider never said."""
        if not self.saw_usage:
            return None
        return Usage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)

    def flush_tool_calls(self) -> list[ToolCall]:
        """Parse every buffered call. Raises rather than emitting a half-read one."""
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            try:
                arguments = json.loads(partial.arguments or "{}")
            except ValueError as exc:
                raise ProviderProtocolError(
                    self.provider_id, f"the arguments for {partial.name!r} were not JSON"
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderProtocolError(
                    self.provider_id, f"the arguments for {partial.name!r} were not an object"
                )
            calls.append(ToolCall(id=partial.id, name=partial.name, arguments=arguments))
        self._calls.clear()
        return calls


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """A `ModelProvider` over the Anthropic Messages API.

    The HTTP client is shared across calls, because a fresh TLS handshake on every step
    of the agent loop is latency the user watches. It holds no key: credentials are a
    per-request header. `aclose()` releases the pool; the router (`P1-08`) owns the
    instance and its lifetime.
    """

    def __init__(
        self,
        key_lookup: KeyLookup,
        *,
        base_url: str = BASE_URL,
        models: Mapping[str, Capabilities] = ANTHROPIC_MODELS,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.id: ProviderId = PROVIDER
        self._base_url = base_url
        self._models = models
        self._key_lookup = key_lookup
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = new_client(self._base_url, self._transport)
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call more than once."""
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # -- contract ----------------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        return self._models.get(model, ANTHROPIC_UNKNOWN)

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        caps = self.capabilities(req.model)
        self._gate(req, caps)
        body = wire_body(req)
        stream = MessageStream(self.id)
        deadline = time.monotonic() + req.timeout_s

        # The whole exchange is inside one translation block: connecting, the status
        # line and every read. Nothing but a `ProviderError` may escape an adapter, and
        # a connection that fails before the first byte is as transient as one that
        # fails after it.
        try:
            async with self._http().stream(
                "POST",
                "/messages",
                json=body,
                headers=self._headers(),
                # A per-read timeout as well as the deadline below: a provider that
                # holds the socket open and sends nothing must not hang the agent loop.
                timeout=httpx2.Timeout(req.timeout_s, connect=CONNECT_TIMEOUT_S),
            ) as response:
                if response.status_code != httpx2.codes.OK:
                    raise status_error(self.id, response.status_code)
                if media_type(response) != EVENT_STREAM:
                    raise ProviderProtocolError(self.id, "the answer was not an event stream")
                log.debug("model.stream_open", extra={"provider": self.id, "model": req.model})
                events = sse_data(response, self.id)
                try:
                    async for data in events:
                        if time.monotonic() > deadline:
                            raise ProviderTransientError(
                                self.id, "the model took too long to answer"
                            )
                        for delta in stream.read(data):
                            yield delta
                finally:
                    # Close the reader while the response is still open. Abandoning the
                    # stream (preemption, a budget breach) leaves it suspended, and an
                    # event loop that finalises it after the socket has gone logs an
                    # error on every stop.
                    await events.aclose()
        except httpx2.TimeoutException as exc:
            raise ProviderTransientError(self.id, "the model took too long to answer") from exc
        except httpx2.HTTPError as exc:
            raise ProviderTransientError(self.id, "the connection to the provider failed") from exc

        for call in stream.flush_tool_calls():
            yield ToolCallDelta(call=call)
        usage = stream.usage()
        if usage is not None:
            yield UsageDelta(usage=usage.model_copy(update={"cost_cents": cost_cents(caps, usage)}))
        if stream.finish_reason is None:
            raise ProviderProtocolError(self.id, "the stream ended without a finish reason")
        yield DoneDelta(finish_reason=stream.finish_reason)

    async def validate_key(self) -> KeyStatus:
        key = self._key_lookup()
        if not key:
            return KeyStatus(valid=False, detail="No key is saved for this provider yet.")
        try:
            response = await self._http().get(
                "/models",
                headers=self._headers(),
                timeout=httpx2.Timeout(CONNECT_TIMEOUT_S),
            )
        except httpx2.HTTPError:
            return KeyStatus(valid=False, detail="Could not reach the provider.")
        if response.status_code == httpx2.codes.OK:
            return KeyStatus(valid=True, detail="The key works.")
        if response.status_code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            return KeyStatus(valid=False, detail="That key was rejected.")
        return KeyStatus(
            valid=False,
            detail=f"The provider answered with an error ({response.status_code}).",
        )

    # -- internals ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Read the key, build the headers, keep nothing.

        Anthropic authenticates with `x-api-key`, not a bearer token.
        """
        key = self._key_lookup()
        if not key:
            raise ProviderAuthError(self.id, "no API key is saved for this provider")
        return {
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": EVENT_STREAM,
        }

    def _gate(self, req: ChatRequest, caps: Capabilities) -> None:
        """Refuse what this model or this API cannot do, here rather than as a 400.

        The router gates a whole task at start (`ARCHITECTURE.md § 5.3`); this is the
        same check one call from the wire, so a wrong `Capabilities` table fails as a
        `ProviderCapabilityError` and not as something retryable.
        """
        if req.tools and not caps.tool_calling:
            raise ProviderCapabilityError(self.id, f"{req.model} cannot call tools")
        if req.json_mode:
            raise ProviderCapabilityError(self.id, f"{req.model} has no JSON mode")
        if not caps.vision and _carries_an_image(req):
            raise ProviderCapabilityError(self.id, f"{req.model} cannot see images")
        if req.temperature is not None and req.temperature > MAX_TEMPERATURE:
            raise ProviderCapabilityError(self.id, "this API takes a temperature between 0 and 1")


def _carries_an_image(req: ChatRequest) -> bool:
    """Only a user message and a tool result can hold one; the other two are text."""
    for message in req.messages:
        if isinstance(message, UserMessage | ToolResultMessage) and any(
            isinstance(part, ImagePart) for part in message.content
        ):
            return True
    return False
