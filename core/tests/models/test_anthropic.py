"""The Anthropic Messages adapter (`P1-03`, `ARCHITECTURE.md § 5.1`).

`REVIEW.md § 6` asks model adapters for recorded-response tests plus one live smoke test
skipped without a key. The recordings here are served by an `httpx2.MockTransport`, so
the whole wire format — the named event sequence, tool arguments arriving as
`input_json_delta` fragments, usage split between the first event and the last, every
error status — is exercised with no network at all.
`test_a_live_call_streams_an_answer` is the live one.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from typing import Any

import httpx2
import pytest
from aegis_core.models.provider import (
    ProviderAuthError,
    ProviderCapabilityError,
    ProviderError,
    ProviderProtocolError,
    ProviderTransientError,
)
from aegis_core.models.providers.anthropic import (
    ANTHROPIC_MODELS,
    ANTHROPIC_UNKNOWN,
    API_VERSION,
    DEFAULT_MAX_TOKENS,
    AnthropicProvider,
    MessageStream,
    wire_body,
)
from aegis_core.models.providers.common import MAX_TOOL_ARGUMENT_CHARS, MAX_TOOL_CALLS
from aegis_core.models.schemas import (
    AssistantMessage,
    Capabilities,
    ChatDelta,
    ChatRequest,
    DoneDelta,
    ImagePart,
    SystemMessage,
    TextPart,
    ToolCall,
    ToolCallDelta,
    ToolDef,
    ToolResultMessage,
    UsageDelta,
    UserMessage,
)

A_KEY = "sk-ant-test-key-do-not-use-0123456789"

MODEL = "claude-sonnet-4-5"
TEXT_ONLY = "text-only"
TEST_MODELS = {
    MODEL: ANTHROPIC_MODELS[MODEL],
    TEXT_ONLY: Capabilities(vision=False, tool_calling=False, json_mode=False, ctx_window=8_192),
}

A_TOOL = ToolDef(
    name="fs.list_dir",
    description="List a folder.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def a_request(**overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "model": MODEL,
        "messages": (UserMessage(content=(TextPart(text="hello"),)),),
    }
    return ChatRequest(**(base | overrides))


# ---------------------------------------------------------------------------
# A recorded stream
# ---------------------------------------------------------------------------


def sse(*events: object) -> list[bytes]:
    """Render events the way the Messages API does, `event:` line included."""
    lines = []
    for event in events:
        name = event["type"] if isinstance(event, dict) else "message"
        lines.append(f"event: {name}\ndata: {json.dumps(event)}\n\n".encode())
    return lines


def message_start(input_tokens: int = 0) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }


def text_block(index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def text_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def tool_block(
    index: int = 0, call_id: str = "toolu_1", name: str = "fs.list_dir"
) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
    }


def tool_delta(partial: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial},
    }


def block_stop(index: int = 0) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def message_delta(stop_reason: str = "end_turn", output_tokens: int = 0) -> dict[str, Any]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }


MESSAGE_STOP: dict[str, Any] = {"type": "message_stop"}


class RecordedStream(httpx2.AsyncByteStream):
    """Bytes for one response, remembering whether the connection was released."""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Recorder:
    """Serves recorded responses and keeps the requests it was asked for."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.streams: list[RecordedStream] = []
        self._respond: Callable[[httpx2.Request], httpx2.Response] = lambda _: httpx2.Response(200)

    def stream(self, *events: object, status: int = 200, content_type: str | None = None) -> None:
        self.raw(sse(*events), status=status, content_type=content_type)

    def raw(
        self,
        body: Sequence[bytes],
        *,
        status: int = 200,
        content_type: str | None = None,
    ) -> None:
        """Serve exact bytes, so a test can choose where the chunk boundaries fall."""

        def respond(_: httpx2.Request) -> httpx2.Response:
            recorded = RecordedStream(body)
            self.streams.append(recorded)
            return httpx2.Response(
                status,
                headers={"content-type": content_type or "text/event-stream"},
                stream=recorded,
            )

        self._respond = respond

    def status(self, status: int, body: str = "") -> None:
        self._respond = lambda _: httpx2.Response(status, text=body)

    def transport(self) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return self._respond(request)

        return httpx2.MockTransport(handler)


def provider_over(recorder: Recorder, *, key: str | None = A_KEY) -> AnthropicProvider:
    return AnthropicProvider(
        lambda: key,
        base_url="https://api.example.invalid/v1",
        models=TEST_MODELS,
        transport=recorder.transport(),
    )


async def collect(stream: AsyncIterator[ChatDelta]) -> list[ChatDelta]:
    return [delta async for delta in stream]


def an_answer(text: str = "Hello") -> tuple[dict[str, Any], ...]:
    """The shortest complete, well-formed stream."""
    return (
        message_start(10),
        text_block(),
        text_delta(text),
        block_stop(),
        message_delta(output_tokens=4),
        MESSAGE_STOP,
    )


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------


def test_a_text_request_becomes_a_streaming_message() -> None:
    body = wire_body(a_request())

    assert body["model"] == MODEL
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    # `max_tokens` is required by this API, so it is always present.
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert "system" not in body


def test_max_tokens_is_the_requested_one_when_there_is_one() -> None:
    assert wire_body(a_request(max_output_tokens=64))["max_tokens"] == 64


def test_the_system_prompt_is_lifted_out_of_the_conversation() -> None:
    """There is no `system` role on the wire here; it is a top-level field."""
    body = wire_body(
        a_request(
            messages=(
                SystemMessage(content="be brief"),
                UserMessage(content=(TextPart(text="go"),)),
                SystemMessage(content="and polite"),
            )
        )
    )

    assert body["system"] == "be brief\n\nand polite"
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_an_image_becomes_a_base64_source_block() -> None:
    body = wire_body(
        a_request(
            messages=(
                UserMessage(
                    content=(
                        TextPart(text="what is this?"),
                        ImagePart(media_type="image/webp", data="QUJD"),
                    )
                ),
            )
        )
    )

    assert body["messages"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/webp", "data": "QUJD"},
    }


def test_an_assistant_turn_carries_tool_use_blocks_with_parsed_input() -> None:
    """Unlike Chat Completions, the arguments are an object, not a JSON string."""
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="list it"),)),
                AssistantMessage(
                    content=(TextPart(text="on it"),),
                    tool_calls=(
                        ToolCall(id="toolu_1", name="fs.list_dir", arguments={"path": "C:\\"}),
                    ),
                ),
            )
        )
    )

    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "on it"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "fs.list_dir",
                "input": {"path": "C:\\"},
            },
        ],
    }


def test_a_tool_result_is_a_block_inside_a_user_turn() -> None:
    body = wire_body(
        a_request(
            messages=(
                ToolResultMessage(tool_call_id="toolu_1", content=(TextPart(text="one file"),)),
            )
        )
    )

    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": "one file"}],
                }
            ],
        }
    ]


def test_a_failed_tool_says_so_on_the_wire() -> None:
    body = wire_body(
        a_request(
            messages=(
                ToolResultMessage(
                    tool_call_id="toolu_1",
                    content=(TextPart(text="access denied"),),
                    is_error=True,
                ),
            )
        )
    )

    assert body["messages"][0]["content"][0]["is_error"] is True


def test_an_image_in_a_tool_result_survives_here() -> None:
    """Chat Completions cannot carry one and `P1-02` refuses it; this API can."""
    body = wire_body(
        a_request(
            messages=(
                ToolResultMessage(
                    tool_call_id="toolu_1",
                    content=(ImagePart(media_type="image/png", data="QUJD"),),
                ),
            )
        )
    )

    block = body["messages"][0]["content"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["data"] == "QUJD"


def test_adjacent_same_role_turns_are_merged() -> None:
    """A tool result is a user turn, so a step that ends with one makes two in a row."""
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(
                    tool_calls=(ToolCall(id="toolu_1", name="fs.list_dir", arguments={}),),
                ),
                ToolResultMessage(tool_call_id="toolu_1", content=(TextPart(text="done"),)),
                ToolResultMessage(tool_call_id="toolu_2", content=(TextPart(text="also done"),)),
                UserMessage(content=(TextPart(text="thanks"),)),
            )
        )
    )

    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    # Both results and the follow-up text ended up in one turn, in order.
    assert [block["type"] for block in body["messages"][2]["content"]] == [
        "tool_result",
        "tool_result",
        "text",
    ]


def test_tools_and_options_are_only_sent_when_asked_for() -> None:
    bare = wire_body(a_request())

    assert "tools" not in bare
    assert "tool_choice" not in bare
    assert "temperature" not in bare
    assert "stop_sequences" not in bare

    full = wire_body(
        a_request(
            tools=(A_TOOL,),
            tool_choice="required",
            temperature=0.2,
            stop=("STOP",),
        )
    )

    assert full["tools"] == [
        {
            "name": "fs.list_dir",
            "description": "List a folder.",
            "input_schema": A_TOOL.parameters,
        }
    ]
    # `required` is spelt `any` here.
    assert full["tool_choice"] == {"type": "any"}
    assert full["temperature"] == 0.2
    assert full["stop_sequences"] == ["STOP"]


@pytest.mark.parametrize(
    ("choice", "wire"),
    [("auto", "auto"), ("none", "none"), ("required", "any")],
)
def test_every_tool_choice_has_a_spelling(choice: str, wire: str) -> None:
    body = wire_body(a_request(tools=(A_TOOL,), tool_choice=choice))

    assert body["tool_choice"] == {"type": wire}


# ---------------------------------------------------------------------------
# Capabilities and the capability gate
# ---------------------------------------------------------------------------


def test_a_model_this_build_has_not_heard_of_still_works_but_has_no_price() -> None:
    caps = AnthropicProvider(lambda: A_KEY).capabilities("claude-something-newer")

    assert caps == ANTHROPIC_UNKNOWN
    assert caps.vision is True
    # An unknown price is unknown, never free.
    assert caps.cost_per_mtok_input is None


def test_the_shipped_table_prices_every_model_it_lists_and_claims_no_json_mode() -> None:
    for model, caps in ANTHROPIC_MODELS.items():
        assert caps.cost_per_mtok_input is not None, model
        assert caps.cost_per_mtok_output is not None, model
        assert caps.cost_per_mtok_output >= caps.cost_per_mtok_input, model
        # This API has no JSON mode at all. See the module docstring.
        assert caps.json_mode is False, model


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"model": TEXT_ONLY, "tools": (A_TOOL,)}, "cannot call tools"),
        ({"json_mode": True}, "has no JSON mode"),
        (
            {
                "model": TEXT_ONLY,
                "messages": (
                    UserMessage(content=(ImagePart(media_type="image/webp", data="QUJD"),)),
                ),
            },
            "cannot see images",
        ),
        ({"temperature": 1.5}, "between 0 and 1"),
    ],
)
async def test_the_capability_gate_refuses_before_any_request(
    overrides: dict[str, Any], why: str
) -> None:
    recorder = Recorder()
    provider = provider_over(recorder)

    with pytest.raises(ProviderCapabilityError, match=why):
        await collect(provider.chat(a_request(**overrides)))

    assert recorder.requests == []


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_a_streamed_answer_arrives_as_text_then_usage_then_done() -> None:
    recorder = Recorder()
    recorder.stream(
        message_start(10),
        text_block(),
        text_delta("Hel"),
        text_delta("lo"),
        block_stop(),
        message_delta(output_tokens=4),
        MESSAGE_STOP,
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["text", "text", "usage", "done"]
    assert "".join(d.text for d in deltas if d.type == "text") == "Hello"
    assert sum(d.type == "done" for d in deltas) == 1
    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "stop"
    await provider.aclose()


async def test_usage_is_assembled_from_both_ends_and_priced_from_the_table() -> None:
    """`input_tokens` arrives with the first event and `output_tokens` with the last."""
    recorder = Recorder()
    recorder.stream(
        message_start(1_000_000),
        text_block(),
        text_delta("hi"),
        message_delta(output_tokens=1_000_000),
        MESSAGE_STOP,
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    assert usage.input_tokens == 1_000_000
    assert usage.output_tokens == 1_000_000
    # 3.00 + 15.00 US dollars, in cents.
    assert usage.cost_cents == pytest.approx(1_800.0)
    await provider.aclose()


async def test_cached_tokens_are_counted_as_input_rather_than_dropped() -> None:
    recorder = Recorder()
    recorder.stream(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 1_000,
                    "output_tokens": 0,
                }
            },
        },
        message_delta(output_tokens=5),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    assert usage.input_tokens == 1_110
    await provider.aclose()


async def test_a_model_that_reports_no_usage_produces_no_usage_delta() -> None:
    recorder = Recorder()
    recorder.stream(
        {"type": "message_start", "message": {}},
        text_block(),
        text_delta("hi"),
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["text", "done"]
    await provider.aclose()


async def test_a_tool_call_is_emitted_once_whole_never_in_fragments() -> None:
    recorder = Recorder()
    recorder.stream(
        message_start(),
        tool_block(),
        tool_delta('{"pa'),
        tool_delta('th": "C:\\\\"}'),
        block_stop(),
        message_delta("tool_use", output_tokens=7),
        MESSAGE_STOP,
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    assert [d.type for d in deltas] == ["tool_call", "usage", "done"]
    call = next(d for d in deltas if isinstance(d, ToolCallDelta)).call
    assert call == ToolCall(id="toolu_1", name="fs.list_dir", arguments={"path": "C:\\"})
    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "tool_calls"
    await provider.aclose()


async def test_a_tool_call_with_no_arguments_is_an_empty_object() -> None:
    """The API sends no `input_json_delta` at all for a tool that takes nothing."""
    recorder = Recorder()
    recorder.stream(
        message_start(),
        tool_block(name="screen.observe"),
        block_stop(),
        message_delta("tool_use"),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    call = next(d for d in deltas if isinstance(d, ToolCallDelta)).call
    assert call.arguments == {}
    await provider.aclose()


async def test_text_and_several_tool_calls_come_back_in_block_order() -> None:
    recorder = Recorder()
    recorder.stream(
        message_start(),
        text_block(0),
        text_delta("doing two things", 0),
        block_stop(0),
        tool_block(1, call_id="toolu_a", name="first"),
        tool_delta('{"a": 1}', 1),
        block_stop(1),
        tool_block(2, call_id="toolu_b", name="second"),
        tool_delta('{"b": 2}', 2),
        block_stop(2),
        message_delta("tool_use"),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    assert [d.type for d in deltas] == ["text", "tool_call", "tool_call", "usage", "done"]
    calls = [d.call for d in deltas if isinstance(d, ToolCallDelta)]
    assert [c.name for c in calls] == ["first", "second"]
    assert [c.arguments for c in calls] == [{"a": 1}, {"b": 2}]
    await provider.aclose()


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
    ],
)
async def test_every_stop_reason_maps_to_a_finish_reason(
    stop_reason: str, finish_reason: str
) -> None:
    recorder = Recorder()
    recorder.stream(message_start(), message_delta(stop_reason))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == finish_reason
    await provider.aclose()


async def test_the_key_is_sent_as_x_api_key_and_read_every_call() -> None:
    recorder = Recorder()
    recorder.stream(*an_answer())
    reads = 0

    def lookup() -> str:
        nonlocal reads
        reads += 1
        return A_KEY

    provider = AnthropicProvider(
        lookup,
        base_url="https://api.example.invalid/v1",
        models=TEST_MODELS,
        transport=recorder.transport(),
    )

    await collect(provider.chat(a_request()))
    await collect(provider.chat(a_request()))

    # This API authenticates with a header of its own, not a bearer token.
    assert recorder.requests[0].headers["x-api-key"] == A_KEY
    assert "authorization" not in recorder.requests[0].headers
    assert recorder.requests[0].headers["anthropic-version"] == API_VERSION
    assert reads == 2
    # Nothing that outlives a call holds a key (`ARCHITECTURE.md § 5.3`).
    assert A_KEY not in repr(vars(provider))
    await provider.aclose()


async def test_no_key_is_an_auth_error_and_never_reaches_the_wire() -> None:
    recorder = Recorder()
    recorder.stream(*an_answer())
    provider = provider_over(recorder, key=None)

    with pytest.raises(ProviderAuthError):
        await collect(provider.chat(a_request()))

    assert recorder.requests == []


async def test_abandoning_a_stream_releases_the_connection() -> None:
    """Preemption and a budget breach both walk away from a stream mid-flight."""
    recorder = Recorder()
    recorder.stream(
        message_start(),
        text_block(),
        text_delta("one"),
        text_delta("two"),
        message_delta(),
    )
    provider = provider_over(recorder)

    stream = provider.chat(a_request())
    assert isinstance(stream, AsyncGenerator)
    assert (await anext(stream)).type == "text"
    await stream.aclose()

    assert recorder.streams[0].closed is True
    await provider.aclose()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (401, ProviderAuthError, False),
        (403, ProviderAuthError, False),
        (404, ProviderCapabilityError, False),
        (429, ProviderTransientError, True),
        (500, ProviderTransientError, True),
        # Anthropic's own "overloaded" status.
        (529, ProviderTransientError, True),
        (400, ProviderProtocolError, False),
    ],
)
async def test_each_status_maps_to_the_right_error(
    status: int, error_type: type[ProviderError], retryable: bool
) -> None:
    recorder = Recorder()
    recorder.status(status)
    provider = provider_over(recorder)

    with pytest.raises(error_type) as caught:
        await collect(provider.chat(a_request()))

    assert caught.value.status == status
    assert caught.value.retryable is retryable
    await provider.aclose()


async def test_an_error_never_carries_the_response_body() -> None:
    """A provider's error body is the likeliest place for a key to be echoed back."""
    recorder = Recorder()
    recorder.status(401, body=json.dumps({"error": {"message": f"bad key {A_KEY}"}}))
    provider = provider_over(recorder)

    with pytest.raises(ProviderAuthError) as caught:
        await collect(provider.chat(a_request()))

    assert A_KEY not in str(caught.value)
    assert "bad key" not in str(caught.value)
    await provider.aclose()


async def test_an_overloaded_error_mid_stream_is_transient_so_the_router_falls_back() -> None:
    """This API can fail *after* a 200, which a status map alone would never see."""
    recorder = Recorder()
    recorder.stream(
        message_start(),
        text_block(),
        text_delta("starting"),
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    provider = provider_over(recorder)

    with pytest.raises(ProviderTransientError) as caught:
        await collect(provider.chat(a_request()))

    assert caught.value.retryable is True
    await provider.aclose()


async def test_an_error_event_never_quotes_the_provider_prose() -> None:
    recorder = Recorder()
    recorder.stream(
        message_start(),
        {"type": "error", "error": {"type": "invalid_request_error", "message": A_KEY}},
    )
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError) as caught:
        await collect(provider.chat(a_request()))

    assert A_KEY not in str(caught.value)
    assert caught.value.retryable is False
    await provider.aclose()


async def test_a_timeout_is_transient_so_the_router_falls_back() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    provider = AnthropicProvider(
        lambda: A_KEY, models=TEST_MODELS, transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(ProviderTransientError) as caught:
        await collect(provider.chat(a_request()))

    assert caught.value.retryable is True
    await provider.aclose()


async def test_a_connection_that_dies_mid_answer_is_transient() -> None:
    class BrokenStream(RecordedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"data: " + json.dumps(text_block()).encode() + b"\n\n"
            raise httpx2.ReadError("connection reset")

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream([])
        )

    provider = AnthropicProvider(
        lambda: A_KEY, models=TEST_MODELS, transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(ProviderTransientError):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_the_reader_handles_the_sse_format_the_api_really_sends() -> None:
    """Events split across reads, keep-alive pings, CRLF, and no trailing blank line."""
    recorder = Recorder()
    first = json.dumps(text_delta("split "))
    second = json.dumps(text_delta("across reads"))
    recorder.raw(
        [
            b": keep-alive\n\n",
            b"event: message_start\ndata: " + json.dumps(message_start(1)).encode() + b"\n\n",
            b"event: content_block_start\ndata: " + json.dumps(text_block()).encode() + b"\n\n",
            f"event: content_block_delta\ndata: {first}".encode(),
            b"\n\r\n",
            f"event: ping\ndata: {json.dumps({'type': 'ping'})}\r\n\r\n".encode(),
            f"event: content_block_delta\r\ndata: {second}\r\n\r\n".encode(),
            # No blank line after the last event.
            f"event: message_delta\ndata: {json.dumps(message_delta(output_tokens=3))}".encode(),
        ]
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["text", "text", "usage", "done"]
    assert "".join(d.text for d in deltas if d.type == "text") == "split across reads"
    await provider.aclose()


async def test_one_endless_event_is_cut_off_rather_than_buffered() -> None:
    """`REVIEW.md § 2`: no unbounded buffer, not even one the provider fills."""
    recorder = Recorder()
    recorder.raw([b"data: " + b"x" * 64_000 for _ in range(20)])
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError, match="too large"):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_a_response_that_is_not_an_event_stream_is_a_protocol_error() -> None:
    recorder = Recorder()
    recorder.stream(*an_answer(), content_type="application/json")
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_a_stream_without_a_stop_reason_is_a_protocol_error() -> None:
    recorder = Recorder()
    recorder.stream(message_start(), text_block(), text_delta("half an answer"))
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError, match="finish reason"):
        await collect(provider.chat(a_request()))

    await provider.aclose()


# ---------------------------------------------------------------------------
# The stream reader on its own
# ---------------------------------------------------------------------------


def test_the_reader_refuses_what_it_cannot_read() -> None:
    with pytest.raises(ProviderProtocolError, match="not JSON"):
        MessageStream().read("{not json")

    with pytest.raises(ProviderProtocolError, match="not an object"):
        MessageStream().read("[]")

    with pytest.raises(ProviderProtocolError, match="no type"):
        MessageStream().read(json.dumps({"index": 0}))

    with pytest.raises(ProviderProtocolError, match="unknown stop reason"):
        MessageStream().read(json.dumps(message_delta("exploded")))


def test_an_event_type_added_after_the_pinned_version_is_ignored() -> None:
    """A live task must not die because the API grew an event this build has not met."""
    stream = MessageStream()

    assert stream.read(json.dumps({"type": "message_stop"})) == []
    assert stream.read(json.dumps({"type": "ping"})) == []
    assert stream.read(json.dumps({"type": "something_new", "index": 0})) == []


def test_a_thinking_block_is_ignored_rather_than_read_as_text() -> None:
    """Nothing asks for one, and its deltas must not become the model's answer."""
    stream = MessageStream()
    stream.read(
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
    )

    assert stream.read(json.dumps(text_delta("private reasoning", 0))) == []


def test_a_tool_call_with_no_name_or_id_is_refused() -> None:
    stream = MessageStream()

    with pytest.raises(ProviderProtocolError, match="no name or id"):
        stream.read(
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "input": {}},
                }
            )
        )


def test_tool_arguments_that_never_parse_are_refused_not_guessed() -> None:
    stream = MessageStream()
    stream.read(json.dumps(tool_block()))
    stream.read(json.dumps(tool_delta("{oops")))

    with pytest.raises(ProviderProtocolError, match="not JSON"):
        stream.flush_tool_calls()


def test_tool_arguments_that_are_not_an_object_are_refused() -> None:
    stream = MessageStream()
    stream.read(json.dumps(tool_block()))
    stream.read(json.dumps(tool_delta("[1, 2]")))

    with pytest.raises(ProviderProtocolError, match="not an object"):
        stream.flush_tool_calls()


def test_a_block_without_an_index_is_refused() -> None:
    stream = MessageStream()

    with pytest.raises(ProviderProtocolError, match="no index"):
        stream.read(json.dumps({"type": "content_block_start", "content_block": {"type": "text"}}))


def test_the_tool_call_buffer_is_bounded() -> None:
    """`REVIEW.md § 2`: no unbounded buffer, not even one a model fills."""
    stream = MessageStream()
    stream.read(json.dumps(tool_block()))
    fragment = "x" * 4096
    with pytest.raises(ProviderProtocolError, match="too large"):
        for _ in range((MAX_TOOL_ARGUMENT_CHARS // len(fragment)) + 2):
            stream.read(json.dumps(tool_delta(fragment)))

    crowd = MessageStream()
    with pytest.raises(ProviderProtocolError, match="too many tools"):
        for index in range(MAX_TOOL_CALLS + 1):
            crowd.read(json.dumps(tool_block(index, call_id=f"toolu_{index}")))


# ---------------------------------------------------------------------------
# validate_key
# ---------------------------------------------------------------------------


async def test_validate_key_answers_the_test_button_without_raising() -> None:
    recorder = Recorder()
    recorder.status(200, body='{"data": []}')
    provider = provider_over(recorder)

    assert (await provider.validate_key()).valid is True
    assert recorder.requests[-1].url.path.endswith("/models")

    recorder.status(401, body=f"bad key {A_KEY}")
    rejected = await provider.validate_key()
    assert rejected.valid is False
    assert A_KEY not in rejected.detail

    recorder.status(500)
    assert (await provider.validate_key()).valid is False

    await provider.aclose()


async def test_validate_key_with_no_key_says_so() -> None:
    recorder = Recorder()
    provider = provider_over(recorder, key=None)

    status = await provider.validate_key()

    assert status.valid is False
    assert recorder.requests == []


async def test_validate_key_survives_an_unreachable_provider() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    provider = AnthropicProvider(lambda: A_KEY, transport=httpx2.MockTransport(handler))

    assert (await provider.validate_key()).valid is False
    await provider.aclose()


# ---------------------------------------------------------------------------
# Live smoke test (`REVIEW.md § 6`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AEGIS_ANTHROPIC_TEST_KEY"),
    reason="set AEGIS_ANTHROPIC_TEST_KEY to run the live smoke test",
)
async def test_a_live_call_streams_an_answer() -> None:
    provider = AnthropicProvider(lambda: os.environ.get("AEGIS_ANTHROPIC_TEST_KEY"))
    try:
        assert (await provider.validate_key()).valid is True

        deltas = await collect(
            provider.chat(
                ChatRequest(
                    model="claude-haiku-4-5",
                    messages=(UserMessage(content=(TextPart(text="Say OK and nothing else."),)),),
                    max_output_tokens=8,
                    timeout_s=30,
                )
            )
        )
    finally:
        await provider.aclose()

    assert deltas[-1].type == "done"
    assert "".join(d.text for d in deltas if d.type == "text").strip()
