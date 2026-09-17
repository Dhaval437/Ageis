"""The data shapes every model adapter speaks (`ARCHITECTURE.md § 5.1`).

These are **internal** to the core. Unlike `server/schemas.py` they are not part of the
HTTP or WebSocket surface, so `aegis_core.server.typegen` does not render them and there
is no TypeScript mirror. The Models screen (`P1-10`) gets what it needs through
`server/schemas.py`, not from here.

Three things travel between the router and an adapter:

- a `ChatRequest` in, carrying the whole conversation and the tools on offer;
- a stream of `ChatDelta` out, ending in exactly one `DoneDelta`;
- a `Capabilities` describing one model, so the router can refuse a task at start
  rather than mid-run (`ARCHITECTURE.md § 5.3`, capability gate).

Everything is frozen and `extra="forbid"`: a provider that invents a field fails at the
boundary rather than somewhere downstream. Sequences are tuples so a frozen model really
is immutable.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

ProviderId = Literal[
    "openai",
    "anthropic",
    "google",
    "nvidia",
    "openrouter",
    "ollama",
    "custom",
]
"""The seven adapters shipped in v1 (`ARCHITECTURE.md § 5.1`)."""


MessageRole = Literal["system", "user", "assistant", "tool"]
"""Who a message came from. The tag of the `ChatMessage` union."""


ImageMediaType = Literal["image/webp", "image/png", "image/jpeg"]
"""What an `ImagePart` may carry. Observations are WebP (`P2-02`)."""


ToolChoice = Literal["auto", "none", "required"]
"""Whether the model may, must not, or must call a tool this turn."""


FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]
"""Why the model stopped. A provider error is an exception, never a finish reason."""


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    """A run of text inside a message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """One image inside a user or tool message.

    `data` is base64 and is **not** included in `repr`: an observation is hundreds of
    kilobytes, and a model-layer error that echoed a request would otherwise put the
    user's screen into a log line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image"] = "image"
    media_type: ImageMediaType
    data: str = Field(repr=False, description="Base64-encoded image bytes, no data: prefix.")


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]
"""One piece of a message body."""


class ToolDef(BaseModel):
    """A tool offered to the model for this turn.

    `parameters` is a JSON Schema object. It is what the adapter sends; it is **not**
    what validates the model's answer — the tool registry revalidates every call before
    the Guardian sees it (`REVIEW.md § 5`, trust the model as little as possible).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1, description="Written for the model, not the user.")
    parameters: dict[str, JsonValue] = Field(description="JSON Schema for the arguments.")


class ToolCall(BaseModel):
    """One tool call the model asked for.

    `arguments` is already parsed. Providers stream tool arguments as JSON fragments;
    the adapter buffers them and emits a `ToolCallDelta` only once the object parses, so
    nothing downstream ever sees half a call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="The provider's id, echoed back in the result.")
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class SystemMessage(BaseModel):
    """The system prompt. Text only — no provider accepts an image here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system"] = "system"
    content: str = Field(min_length=1)


class UserMessage(BaseModel):
    """Input from the user, or an observation the agent captured on their behalf.

    Observation text is untrusted (`REVIEW.md § 5`). Framing it as such is the caller's
    job in `P8-02`; this type only carries it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user"] = "user"
    content: tuple[ContentPart, ...] = Field(min_length=1)


class AssistantMessage(BaseModel):
    """What the model said last turn, replayed back to it.

    A turn is text, tool calls, or both, so neither field is required — but an assistant
    message with nothing in it is a bug, and is rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: tuple[TextPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def _not_empty(self) -> AssistantMessage:
        if not self.content and not self.tool_calls:
            raise ValueError("an assistant message needs text, tool calls, or both")
        return self


class ToolResultMessage(BaseModel):
    """The result of running one tool, answering one `ToolCall`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["tool"] = "tool"
    tool_call_id: str = Field(min_length=1)
    content: tuple[ContentPart, ...] = Field(min_length=1)
    is_error: bool = Field(default=False, description="The tool failed; the text says how.")


ChatMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]
"""One message in a conversation, tagged by `role`."""


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """One call to one model.

    The adapter owns the wire format; this owns the meaning. `model` is the provider's
    own id (`gpt-4o`, `llama3.1:8b`) because only the provider can interpret it.

    There is **no key field**. Keys are read from the DPAPI vault at call time by the
    adapter and never held in a long-lived object (`ARCHITECTURE.md § 5.3`), so a request
    that is logged, replayed or put in an error payload cannot carry one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, description="The provider's model id.")
    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    tools: tuple[ToolDef, ...] = ()
    tool_choice: ToolChoice = "auto"
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)
    json_mode: bool = Field(default=False, description="Ask for a JSON object, not prose.")
    stop: tuple[str, ...] = ()
    timeout_s: float = Field(
        default=120.0,
        gt=0,
        description="Wall clock for the whole stream. Every external call has one "
        "(`REVIEW.md § 2`), so this has a default rather than being optional.",
    )


# ---------------------------------------------------------------------------
# Response stream
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    """What the call cost.

    `cost_cents` is `None` when the provider prices nothing we can read — a local
    Ollama model, or a `custom` gateway. It is a float, not integer cents, because one
    cheap call costs a fraction of a cent and integers round a whole task to zero
    (same reason as `tasks.cost_cents` in `ARCHITECTURE.md § 7`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_cents: float | None = Field(default=None, ge=0)


class TextDelta(BaseModel):
    """A fragment of the model's prose, for streaming into the timeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str


class ToolCallDelta(BaseModel):
    """A complete tool call. Never a fragment — see `ToolCall`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class UsageDelta(BaseModel):
    """Token counts, emitted once the provider reports them.

    Separate from `DoneDelta` because providers report usage at different points, and
    some never do — the budget guard (`P1-09`) must not have to wait for the end.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["usage"] = "usage"
    usage: Usage


class DoneDelta(BaseModel):
    """The last delta of a successful stream. Exactly one, always."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["done"] = "done"
    finish_reason: FinishReason


ChatDelta = Annotated[
    TextDelta | ToolCallDelta | UsageDelta | DoneDelta,
    Field(discriminator="type"),
]
"""One item from `ModelProvider.chat()`, tagged by `type`."""


# ---------------------------------------------------------------------------
# Provider description
# ---------------------------------------------------------------------------


class Capabilities(BaseModel):
    """What one model can do, so the router can refuse a task before it starts.

    Costs are split per direction because output is priced several times input
    everywhere, and the budget guard (`P1-09`) has to add them separately. Both are in
    **US dollars per million tokens**, the unit every provider publishes. `None` means
    unknown — a `custom` gateway or a local model — and an unknown price is never
    treated as free.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vision: bool = Field(description="Accepts an `ImagePart`. A `GROUNDER` needs this.")
    tool_calling: bool = Field(description="Accepts `tools` and can answer with a `ToolCall`.")
    json_mode: bool = Field(description="Honours `ChatRequest.json_mode`.")
    ctx_window: int = Field(gt=0, description="Total tokens the model accepts, in + out.")
    cost_per_mtok_input: float | None = Field(default=None, ge=0)
    cost_per_mtok_output: float | None = Field(default=None, ge=0)


class KeyStatus(BaseModel):
    """The answer to "is this key usable?" for the Settings *Test* button.

    `detail` is shown to the user and **must never contain the key**, any part of it, or
    a raw provider response body. Keep it to what went wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    detail: str = Field(description="Plain, second person, no key material.")
