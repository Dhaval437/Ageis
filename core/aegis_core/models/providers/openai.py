"""The OpenAI Chat Completions adapter — and, through it, five of the seven providers.

`ARCHITECTURE.md § 5.1`: `nvidia`, `openrouter` and `custom` are this adapter with a
different base URL and model table, and `ollama` speaks the same dialect on
`localhost:11434`. So everything that differs between them lives in `ProviderConfig`,
and **nothing** in this module names a provider — `P1-05` and `P1-06` add configs, not
clients.

Three obligations from the protocol, and where each is met here:

- **The key is read at call time.** `OpenAIProvider` holds a `KeyLookup`, not a key.
  `_auth_header()` calls it, builds one header and keeps nothing; the vault behind it
  arrives in `P1-07`. A request that is logged or replayed cannot carry a key, because
  `ChatRequest` has no field for one.
- **The stream ends in exactly one `DoneDelta`, or raises a `ProviderError`.** Every
  `httpx2` failure is translated; nothing else escapes.
- **The connection is released in a `finally`.** `client.stream()` is an `async with`
  inside the generator, so abandoning the stream — preemption, a budget breach —
  returns the connection to the pool at the `yield` it was suspended on.

Two things this adapter deliberately does not do. It never puts a response body in an
error: a provider's error body is the most likely place for a key to be echoed back
(`REVIEW.md § 5`), so a `ProviderError` carries a status and a short reason written
here. And it never emits a partial tool call: argument fragments are buffered until the
whole object parses, so the Guardian never sees half a call.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

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
    ChatMessage,
    ChatRequest,
    ContentPart,
    DoneDelta,
    FinishReason,
    ImagePart,
    KeyStatus,
    ProviderId,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallDelta,
    ToolResultMessage,
    Usage,
    UsageDelta,
    UserMessage,
)

log = logging.getLogger(__name__)

_SSE_DONE: Final = "[DONE]"


# ---------------------------------------------------------------------------
# Configuration — the only thing that differs between the compatible providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """One OpenAI-compatible endpoint.

    `unknown_model` is what `capabilities()` answers for a model that is not in
    `models`. It is `None` here — an unknown OpenAI model is a typo — but a gateway
    that serves hundreds of models (`openrouter`, `custom`, `P1-05`) has to answer
    something, and an unknown *price* is `None`, never free.
    """

    id: ProviderId
    base_url: str
    models: Mapping[str, Capabilities]
    unknown_model: Capabilities | None = None
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


def _caps(
    *,
    vision: bool,
    ctx_window: int,
    cost_in: float,
    cost_out: float,
    tool_calling: bool = True,
    json_mode: bool = True,
) -> Capabilities:
    return Capabilities(
        vision=vision,
        tool_calling=tool_calling,
        json_mode=json_mode,
        ctx_window=ctx_window,
        cost_per_mtok_input=cost_in,
        cost_per_mtok_output=cost_out,
    )


#: A snapshot of OpenAI's published list prices, in **US dollars per million tokens**,
#: taken 2026-09-17. Prices move, and a wrong one makes the `P1-09` budget guard wrong
#: in the direction of spending more than the user allowed — so the Models screen
#: (`P1-10`) shows what we think a model costs, and this table is what it shows.
OPENAI_MODELS: Final[Mapping[str, Capabilities]] = {
    "gpt-4o": _caps(vision=True, ctx_window=128_000, cost_in=2.50, cost_out=10.00),
    "gpt-4o-mini": _caps(vision=True, ctx_window=128_000, cost_in=0.15, cost_out=0.60),
    "gpt-4.1": _caps(vision=True, ctx_window=1_047_576, cost_in=2.00, cost_out=8.00),
    "gpt-4.1-mini": _caps(vision=True, ctx_window=1_047_576, cost_in=0.40, cost_out=1.60),
    "gpt-4.1-nano": _caps(vision=True, ctx_window=1_047_576, cost_in=0.10, cost_out=0.40),
    "o3": _caps(vision=True, ctx_window=200_000, cost_in=2.00, cost_out=8.00),
    "o4-mini": _caps(vision=True, ctx_window=200_000, cost_in=1.10, cost_out=4.40),
}


OPENAI: Final = ProviderConfig(
    id="openai",
    base_url="https://api.openai.com/v1",
    models=OPENAI_MODELS,
)


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------


def _text_of(parts: Sequence[ContentPart]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart))


def _wire_part(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{part.media_type};base64,{part.data}"},
    }


def _wire_message(message: ChatMessage, config: ProviderConfig) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": [_wire_part(part) for part in message.content]}

    if isinstance(message, AssistantMessage):
        wire: dict[str, Any] = {"role": "assistant"}
        if message.content:
            wire["content"] = _text_of(message.content)
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
        return wire

    if message.role == "tool":
        # Chat Completions accepts only text in a tool result. Dropping an image here
        # would silently answer a vision question with nothing, so it is an error.
        if any(isinstance(part, ImagePart) for part in message.content):
            raise ProviderCapabilityError(
                config.id, "this API cannot carry an image in a tool result"
            )
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": _text_of(message.content),
        }

    return {"role": "system", "content": message.content}


def wire_body(req: ChatRequest, config: ProviderConfig) -> dict[str, Any]:
    """Build the Chat Completions request body. Pure, so the mapping is testable."""
    body: dict[str, Any] = {
        "model": req.model,
        "messages": [_wire_message(message, config) for message in req.messages],
        "stream": True,
        # Without this, a streamed call reports no usage at all and the budget guard
        # has nothing to count.
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in req.tools
        ]
        body["tool_choice"] = req.tool_choice
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.max_output_tokens is not None:
        body[config.max_tokens_field] = req.max_output_tokens
    if req.json_mode:
        body["response_format"] = {"type": "json_object"}
    if req.stop:
        body["stop"] = list(req.stop)
    return body


# ---------------------------------------------------------------------------
# Wire → deltas
# ---------------------------------------------------------------------------

_FINISH_REASONS: Final[Mapping[str, FinishReason]] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


@dataclass
class _PartialToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ChatStream:
    """Reads one SSE stream into deltas, holding the state a chunk alone cannot carry.

    Separate from the adapter because it is where the awkward parts of the format live —
    tool arguments arriving in fragments, usage arriving after the finish reason — and
    those deserve tests that need no socket.
    """

    provider_id: ProviderId
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    _calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    _argument_chars: int = 0

    def read(self, data: str) -> list[ChatDelta]:
        """Turn one `data:` payload into the deltas it produced, if any."""
        chunk = json_object(data, self.provider_id)

        self._read_usage(chunk.get("usage"))

        deltas: list[ChatDelta] = []
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return deltas
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderProtocolError(self.provider_id, "a stream choice was not an object")

        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = delta.get("content")
            if isinstance(text, str) and text:
                deltas.append(TextDelta(text=text))
            self._read_tool_calls(delta.get("tool_calls"))

        reason = choice.get("finish_reason")
        if isinstance(reason, str):
            self.finish_reason = _FINISH_REASONS.get(reason)
            if self.finish_reason is None:
                raise ProviderProtocolError(
                    self.provider_id, f"unknown finish reason {reason[:32]!r}"
                )
        return deltas

    def _read_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if not isinstance(prompt, int) or not isinstance(completion, int):
            return
        self.usage = Usage(input_tokens=max(prompt, 0), output_tokens=max(completion, 0))

    def _read_tool_calls(self, raw: object) -> None:
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                raise ProviderProtocolError(self.provider_id, "a tool call was not an object")
            index = entry.get("index", 0)
            if not isinstance(index, int):
                raise ProviderProtocolError(self.provider_id, "a tool call had no index")
            partial = self._calls.get(index)
            if partial is None:
                if len(self._calls) >= MAX_TOOL_CALLS:
                    raise ProviderProtocolError(
                        self.provider_id, "the model asked for too many tools at once"
                    )
                partial = _PartialToolCall()
                self._calls[index] = partial

            call_id = entry.get("id")
            if isinstance(call_id, str) and call_id:
                partial.id = call_id
            function = entry.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                partial.name = name
            fragment = function.get("arguments")
            if isinstance(fragment, str) and fragment:
                self._argument_chars += len(fragment)
                if self._argument_chars > MAX_TOOL_ARGUMENT_CHARS:
                    raise ProviderProtocolError(
                        self.provider_id, "the tool arguments were too large to read"
                    )
                partial.arguments += fragment

    def flush_tool_calls(self) -> list[ToolCall]:
        """Parse every buffered call. Raises rather than emitting a half-read one."""
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if not partial.id or not partial.name:
                raise ProviderProtocolError(self.provider_id, "a tool call had no name or id")
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


class OpenAIProvider:
    """A `ModelProvider` over the OpenAI Chat Completions API.

    The HTTP client is shared across calls, because a fresh TLS handshake on every step
    of the agent loop is latency the user watches. It holds no key: credentials are a
    per-request header. `aclose()` releases the pool; the router (`P1-08`) owns the
    instance and its lifetime.
    """

    def __init__(
        self,
        key_lookup: KeyLookup,
        *,
        config: ProviderConfig = OPENAI,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.id: ProviderId = config.id
        self._config = config
        self._key_lookup = key_lookup
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = new_client(self._config.base_url, self._transport)
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call more than once."""
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # -- contract ----------------------------------------------------------

    def capabilities(self, model: str) -> Capabilities:
        caps = self._config.models.get(model, self._config.unknown_model)
        if caps is None:
            raise ProviderCapabilityError(self.id, f"unknown model {model[:64]!r}")
        return caps

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        caps = self.capabilities(req.model)
        self._gate(req, caps)
        body = wire_body(req, self._config)
        stream = ChatStream(self.id)
        deadline = time.monotonic() + req.timeout_s

        # The whole exchange is inside one translation block: connecting, the status
        # line and every read. Nothing but a `ProviderError` may escape an adapter, and
        # a connection that fails before the first byte is as transient as one that
        # fails after it.
        try:
            async with self._http().stream(
                "POST",
                "/chat/completions",
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
                        if data == _SSE_DONE:
                            break
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
        if stream.usage is not None:
            yield UsageDelta(
                usage=stream.usage.model_copy(update={"cost_cents": cost_cents(caps, stream.usage)})
            )
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
        """Read the key, build one header, keep nothing."""
        key = self._key_lookup()
        if not key:
            raise ProviderAuthError(self.id, "no API key is saved for this provider")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _gate(self, req: ChatRequest, caps: Capabilities) -> None:
        """Refuse what the model cannot do, here rather than as a provider 400.

        The router gates a whole task at start (`ARCHITECTURE.md § 5.3`); this is the
        same check one call from the wire, so a wrong `Capabilities` table fails as a
        `ProviderCapabilityError` and not as something retryable.
        """
        if req.tools and not caps.tool_calling:
            raise ProviderCapabilityError(self.id, f"{req.model} cannot call tools")
        if req.json_mode and not caps.json_mode:
            raise ProviderCapabilityError(self.id, f"{req.model} has no JSON mode")
        if not caps.vision and _carries_an_image(req):
            raise ProviderCapabilityError(self.id, f"{req.model} cannot see images")


def _carries_an_image(req: ChatRequest) -> bool:
    """Only a user message and a tool result can hold one; the other two are text."""
    for message in req.messages:
        if isinstance(message, UserMessage | ToolResultMessage) and any(
            isinstance(part, ImagePart) for part in message.content
        ):
            return True
    return False
