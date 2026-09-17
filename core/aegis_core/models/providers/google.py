"""The Google Gemini adapter (`ARCHITECTURE.md § 5.1`, `P1-04`).

The third and last wire format. Everything it shares with the other two — the SSE
reader, the status map, the cost sum, the client — comes from `common.py`. What is left
is Gemini's own vocabulary, and it disagrees with the other two in more places than any
pair of them disagree with each other:

- **The assistant is called `model`**, and there are only two roles. As with Anthropic
  the system prompt is lifted out (`systemInstruction`) and a tool result is a *user*
  turn, so adjacent same-role turns are coalesced.
- **Tool results are addressed by name, not by id.** Gemini has no `tool_call_id`, so
  `_call_names` walks the conversation to turn the id in a `ToolResultMessage` back into
  the name of the call it answers. A result for a call that is not in the conversation
  is refused rather than sent under a guessed name.
- **Tool calls arrive whole.** `functionCall.args` is a JSON object in one part, not a
  string in fragments, so there is nothing to buffer and no half-parsed call to guard
  against. They are still flushed at the end, so the delta order matches every other
  provider.
- **There is no tool-call finish reason.** A turn that asked for a tool finishes `STOP`
  like any other, so the finish reason is decided by what came back, not by what the
  provider called it.
- **Thoughts are text.** A 2.5-series model returns its reasoning as ordinary text parts
  flagged `thought: true`. They are dropped: they are not the answer, and putting them
  in the timeline would show the user the model's scratchpad as if it were a result.

The key goes in a header, never `?key=`. A URL carries into access logs, proxy logs and
crash reports, and `REVIEW.md § 5` is explicit that a key reaches none of those.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

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
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_UNAUTHORIZED,
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

PROVIDER: Final[ProviderId] = "google"

BASE_URL: Final = "https://generativelanguage.googleapis.com/v1beta"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _caps(
    *,
    ctx_window: int = 1_048_576,
    cost_in: float | None,
    cost_out: float | None,
    vision: bool = True,
    tool_calling: bool = True,
) -> Capabilities:
    return Capabilities(
        vision=vision,
        tool_calling=tool_calling,
        # `responseMimeType: application/json`, which this adapter sets for `json_mode`.
        json_mode=True,
        ctx_window=ctx_window,
        cost_per_mtok_input=cost_in,
        cost_per_mtok_output=cost_out,
    )


#: A snapshot of Google's published list prices, in **US dollars per million tokens**,
#: taken 2026-09-17.
#:
#: One simplification worth knowing about: Gemini prices a prompt over 200k tokens at a
#: higher rate than one below it, and `Capabilities` has one price per direction. These
#: are the **under-200k** rates, so a very long prompt is under-reported rather than
#: over-reported. `P1-09` pauses a task on a budget breach, so under-reporting is the
#: direction that can overspend — the note is here so the next session can decide
#: whether tiered pricing is worth a shape change, and `P1-10` shows the user what we
#: think a model costs.
GOOGLE_MODELS: Final[Mapping[str, Capabilities]] = {
    "gemini-2.5-pro": _caps(cost_in=1.25, cost_out=10.00),
    "gemini-2.5-flash": _caps(cost_in=0.30, cost_out=2.50),
    "gemini-2.5-flash-lite": _caps(cost_in=0.10, cost_out=0.40),
    "gemini-2.0-flash": _caps(cost_in=0.10, cost_out=0.40),
    "gemini-2.0-flash-lite": _caps(cost_in=0.075, cost_out=0.30),
}

#: What a model this build has never heard of can be assumed to do, for the same reason
#: `anthropic` has one: Google ships models faster than a pinned table is updated, and
#: refusing a model the user's key can serve helps nobody. A typo still fails, as the
#: API's own 404. The **price is unknown, never free** (`Capabilities`).
GOOGLE_UNKNOWN: Final = _caps(cost_in=None, cost_out=None)


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------

_TOOL_MODE: Final[Mapping[ToolChoice, str]] = {
    "auto": "AUTO",
    "none": "NONE",
    "required": "ANY",
}


def _wire_part(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"text": part.text}
    return {"inlineData": {"mimeType": part.media_type, "data": part.data}}


def _text_of(parts: Sequence[ContentPart]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart))


def _call_names(messages: Iterable[object]) -> dict[str, str]:
    """Map every tool call id in the conversation to the name it called.

    Gemini answers a tool by **name**; there is no id anywhere in its vocabulary. The
    ids in a `ChatRequest` are this adapter's own invention (see `GenerateStream`), so
    the conversation is the only place the name can come from.
    """
    names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                names[call.id] = call.name
    return names


def _wire_message(
    message: UserMessage | AssistantMessage | ToolResultMessage,
    names: Mapping[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    """One message as its wire role and parts. System messages never get here."""
    if isinstance(message, UserMessage):
        return "user", [_wire_part(part) for part in message.content]

    if isinstance(message, AssistantMessage):
        parts = [_wire_part(part) for part in message.content]
        parts.extend(
            {"functionCall": {"name": call.name, "args": call.arguments}}
            for call in message.tool_calls
        )
        # Gemini calls the assistant `model`.
        return "model", parts

    name = names.get(message.tool_call_id)
    if name is None:
        raise ProviderProtocolError(
            PROVIDER, "a tool result answered a call that is not in this conversation"
        )
    key = "error" if message.is_error else "output"
    answer: list[dict[str, Any]] = [
        {"functionResponse": {"name": name, "response": {key: _text_of(message.content)}}}
    ]
    # A `functionResponse.response` is a JSON object and cannot hold image bytes. An
    # observation a tool returned is the answer to a vision question, so rather than
    # drop it (as Chat Completions forced `P1-02` to refuse), it rides along as an
    # ordinary part of the same user turn.
    answer.extend(_wire_part(part) for part in message.content if isinstance(part, ImagePart))
    return "user", answer


def _coalesce(turns: Sequence[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Merge adjacent turns with the same role.

    A tool result is a user turn here too, so a step that ends with one followed by
    anything the user says produces two user turns in a row.
    """
    merged: list[dict[str, Any]] = []
    for role, parts in turns:
        if merged and merged[-1]["role"] == role:
            merged[-1]["parts"].extend(parts)
        else:
            merged.append({"role": role, "parts": list(parts)})
    return merged


def wire_body(req: ChatRequest) -> dict[str, Any]:
    """Build the `generateContent` request body. Pure, so the mapping is testable."""
    system = "\n\n".join(m.content for m in req.messages if isinstance(m, SystemMessage))
    names = _call_names(req.messages)
    turns = [_wire_message(m, names) for m in req.messages if not isinstance(m, SystemMessage)]

    body: dict[str, Any] = {"contents": _coalesce(turns)}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if req.tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in req.tools
                ]
            }
        ]
        body["toolConfig"] = {"functionCallingConfig": {"mode": _TOOL_MODE[req.tool_choice]}}

    config: dict[str, Any] = {}
    if req.temperature is not None:
        config["temperature"] = req.temperature
    if req.max_output_tokens is not None:
        config["maxOutputTokens"] = req.max_output_tokens
    if req.json_mode:
        config["responseMimeType"] = "application/json"
    if req.stop:
        config["stopSequences"] = list(req.stop)
    if config:
        body["generationConfig"] = config
    return body


# ---------------------------------------------------------------------------
# Wire → deltas
# ---------------------------------------------------------------------------

_FINISH_REASONS: Final[Mapping[str, FinishReason]] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "SPII": "content_filter",
}


@dataclass
class GenerateStream:
    """Reads one `streamGenerateContent` SSE stream into deltas.

    Separate from the adapter because this is where Gemini's shape is awkward — text
    arriving as parts inside a candidate, thoughts that look exactly like the answer,
    usage restated on every chunk — and that deserves tests that need no socket.
    """

    provider_id: ProviderId = PROVIDER
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    _calls: list[ToolCall] = field(default_factory=list)

    def read(self, data: str) -> list[ChatDelta]:
        """Turn one `GenerateContentResponse` into the deltas it produced, if any."""
        chunk = json_object(data, self.provider_id)
        self._read_usage(chunk.get("usageMetadata"))
        self._read_prompt_feedback(chunk.get("promptFeedback"))

        candidates = chunk.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return []
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderProtocolError(self.provider_id, "a candidate was not an object")

        deltas = self._read_parts(candidate.get("content"))
        self._read_finish_reason(candidate.get("finishReason"))
        return deltas

    def _read_parts(self, content: object) -> list[ChatDelta]:
        if not isinstance(content, dict):
            return []
        parts = content.get("parts")
        if not isinstance(parts, list):
            return []

        deltas: list[ChatDelta] = []
        for part in parts:
            if not isinstance(part, dict):
                raise ProviderProtocolError(self.provider_id, "a content part was not an object")
            call = part.get("functionCall")
            if isinstance(call, dict):
                self._read_function_call(call)
                continue
            # A 2.5-series model returns its reasoning as a text part flagged this way.
            # It is not the answer, and the user is not shown the model's scratchpad.
            if part.get("thought") is True:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                deltas.append(TextDelta(text=text))
        return deltas

    def _read_function_call(self, call: Mapping[str, object]) -> None:
        """Collect one whole call. Gemini sends `args` as an object, never in fragments."""
        if len(self._calls) >= MAX_TOOL_CALLS:
            raise ProviderProtocolError(
                self.provider_id, "the model asked for too many tools at once"
            )
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise ProviderProtocolError(self.provider_id, "a tool call had no name")
        arguments = call.get("args")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProviderProtocolError(
                self.provider_id, f"the arguments for {name!r} were not an object"
            )
        # Gemini has no id for a call, and `ToolCall` requires one because every other
        # provider does. It is minted here and resolved back to this name by
        # `_call_names` when the result comes home.
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{len(self._calls) + 1}"
        self._calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

    def _read_usage(self, raw: object) -> None:
        """`usageMetadata` is restated in full on every chunk, so the last one wins."""
        if not isinstance(raw, dict):
            return
        prompt = raw.get("promptTokenCount")
        completion = raw.get("candidatesTokenCount")
        if not isinstance(prompt, int):
            return
        if not isinstance(completion, int):
            completion = 0
        # Thinking tokens are billed as output and are not in `candidatesTokenCount`.
        thoughts = raw.get("thoughtsTokenCount")
        if isinstance(thoughts, int) and thoughts > 0:
            completion += thoughts
        self.usage = Usage(input_tokens=max(prompt, 0), output_tokens=max(completion, 0))

    def _read_prompt_feedback(self, raw: object) -> None:
        """A prompt refused before generation. A refusal is an outcome, not an error."""
        if not isinstance(raw, dict):
            return
        blocked = raw.get("blockReason")
        if isinstance(blocked, str) and blocked:
            self.finish_reason = "content_filter"

    def _read_finish_reason(self, raw: object) -> None:
        if not isinstance(raw, str) or not raw:
            return
        if raw == "MALFORMED_FUNCTION_CALL":
            raise ProviderProtocolError(self.provider_id, "the model asked for a tool it mangled")
        reason = _FINISH_REASONS.get(raw)
        if reason is None:
            # Includes `OTHER` and `FINISH_REASON_UNSPECIFIED`. An answer whose ending
            # cannot be named is one that cannot be reported honestly.
            raise ProviderProtocolError(self.provider_id, f"unknown finish reason {raw[:32]!r}")
        self.finish_reason = reason

    def flush_tool_calls(self) -> list[ToolCall]:
        """Every call collected, in the order the model asked for them."""
        calls, self._calls = self._calls, []
        return calls

    def resolve_finish_reason(self) -> FinishReason | None:
        """What to report, once the whole stream has been read.

        Gemini has no tool-call finish reason: a turn that asked for a tool ends `STOP`
        like any other. So the answer is decided by what came back, not by what the
        provider called it — but a `content_filter` or a `length` still wins, because
        those say the turn was cut short and the tool calls in it may be incomplete.
        """
        if self._calls and self.finish_reason == "stop":
            return "tool_calls"
        return self.finish_reason


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class GoogleProvider:
    """A `ModelProvider` over the Gemini API.

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
        models: Mapping[str, Capabilities] = GOOGLE_MODELS,
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
        return self._models.get(model, GOOGLE_UNKNOWN)

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        caps = self.capabilities(req.model)
        self._gate(req, caps)
        body = wire_body(req)
        stream = GenerateStream(self.id)
        deadline = time.monotonic() + req.timeout_s

        # The whole exchange is inside one translation block: connecting, the status
        # line and every read. Nothing but a `ProviderError` may escape an adapter, and
        # a connection that fails before the first byte is as transient as one that
        # fails after it.
        try:
            async with self._http().stream(
                "POST",
                self._path(req.model, "streamGenerateContent"),
                # `alt=sse` is what makes this a stream of events rather than one very
                # long JSON array that cannot be read until it ends.
                params={"alt": "sse"},
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

        # Resolved *before* the flush: what the turn finished with depends on whether it
        # asked for a tool, and flushing is what empties that record.
        finish_reason = stream.resolve_finish_reason()
        for call in stream.flush_tool_calls():
            yield ToolCallDelta(call=call)
        if stream.usage is not None:
            yield UsageDelta(
                usage=stream.usage.model_copy(update={"cost_cents": cost_cents(caps, stream.usage)})
            )
        if finish_reason is None:
            raise ProviderProtocolError(self.id, "the stream ended without a finish reason")
        yield DoneDelta(finish_reason=finish_reason)

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
        # This API answers a rejected key with **400**, not 401, and the only way to
        # tell which 400 it is would be to read the body — which may quote the key
        # back (`REVIEW.md § 5`). There is nothing to get wrong in a parameterless
        # `GET /models`, so a 400 here is the key.
        if response.status_code in (HTTP_BAD_REQUEST, HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            return KeyStatus(valid=False, detail="That key was rejected.")
        return KeyStatus(
            valid=False,
            detail=f"The provider answered with an error ({response.status_code}).",
        )

    # -- internals ---------------------------------------------------------

    def _path(self, model: str, method: str) -> str:
        """`/models/{model}:{method}`, with the model id escaped.

        The model id reaches here from settings the user typed. It is escaped rather
        than trusted so it cannot add a path segment or a query of its own.
        """
        return f"/models/{quote(model, safe='')}:{method}"

    def _headers(self) -> dict[str, str]:
        """Read the key, build the headers, keep nothing.

        The key goes in `x-goog-api-key`, never in `?key=`: a URL reaches access logs,
        proxy logs and crash reports, and a key reaches none of those.
        """
        key = self._key_lookup()
        if not key:
            raise ProviderAuthError(self.id, "no API key is saved for this provider")
        return {
            "x-goog-api-key": key,
            "Content-Type": "application/json",
            "Accept": EVENT_STREAM,
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
