"""The OpenAI Chat Completions adapter (`P1-02`, `ARCHITECTURE.md § 5.1`).

`REVIEW.md § 6` asks model adapters for recorded-response tests plus one live smoke
test skipped without a key. The recordings here are served by an `httpx2.MockTransport`,
so the whole wire format — streamed text, tool arguments arriving in fragments, usage
after the finish reason, every error status — is exercised with no network at all.
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
from aegis_core.models.providers.common import (
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_CALLS,
    cost_cents,
)
from aegis_core.models.providers.openai import (
    OPENAI,
    ChatStream,
    OpenAIProvider,
    ProviderConfig,
    wire_body,
)
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
    Usage,
    UsageDelta,
    UserMessage,
)

A_KEY = "sk-test-key-do-not-use-0123456789"

VISION_MODEL = "gpt-4o-mini"
TEST_MODELS = {
    VISION_MODEL: Capabilities(
        vision=True,
        tool_calling=True,
        json_mode=True,
        ctx_window=128_000,
        cost_per_mtok_input=0.15,
        cost_per_mtok_output=0.60,
    ),
    "text-only": Capabilities(vision=False, tool_calling=False, json_mode=False, ctx_window=8_192),
}
TEST_CONFIG = ProviderConfig(
    id="openai", base_url="https://api.example.invalid/v1", models=TEST_MODELS
)

A_TOOL = ToolDef(
    name="fs.list_dir",
    description="List a folder.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def a_request(**overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "model": VISION_MODEL,
        "messages": (UserMessage(content=(TextPart(text="hello"),)),),
    }
    return ChatRequest(**(base | overrides))


# ---------------------------------------------------------------------------
# A recorded stream
# ---------------------------------------------------------------------------


def sse(*chunks: object) -> list[bytes]:
    """Render chunks the way a provider does, with the final `[DONE]`."""
    lines = [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]
    return [*lines, b"data: [DONE]\n\n"]


def text_chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def finish_chunk(reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def usage_chunk(prompt: int, completion: int) -> dict[str, Any]:
    return {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


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

    def stream(self, *chunks: object, status: int = 200, content_type: str | None = None) -> None:
        self.raw(sse(*chunks), status=status, content_type=content_type)

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


def provider_over(
    recorder: Recorder,
    *,
    key: str | None = A_KEY,
    config: ProviderConfig = TEST_CONFIG,
) -> OpenAIProvider:
    return OpenAIProvider(lambda: key, config=config, transport=recorder.transport())


async def collect(stream: AsyncIterator[ChatDelta]) -> list[ChatDelta]:
    return [delta async for delta in stream]


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------


def test_a_text_request_becomes_a_streaming_chat_completion() -> None:
    body = wire_body(a_request(), TEST_CONFIG)

    assert body["model"] == VISION_MODEL
    assert body["stream"] is True
    # Without include_usage a streamed call reports nothing for P1-09 to count.
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_an_image_becomes_a_data_url() -> None:
    req = a_request(
        messages=(
            UserMessage(
                content=(
                    TextPart(text="what is this?"),
                    ImagePart(media_type="image/webp", data="QUJD"),
                )
            ),
        )
    )

    parts = wire_body(req, TEST_CONFIG)["messages"][0]["content"]

    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/webp;base64,QUJD"}}


def test_every_role_maps_to_its_wire_shape() -> None:
    req = a_request(
        messages=(
            SystemMessage(content="be brief"),
            UserMessage(content=(TextPart(text="list it"),)),
            AssistantMessage(
                content=(TextPart(text="on it"),),
                tool_calls=(ToolCall(id="c1", name="fs.list_dir", arguments={"path": "C:\\"}),),
            ),
            ToolResultMessage(tool_call_id="c1", content=(TextPart(text="one file"),)),
        )
    )

    messages = wire_body(req, TEST_CONFIG)["messages"]

    assert messages[0] == {"role": "system", "content": "be brief"}
    assert messages[2] == {
        "role": "assistant",
        "content": "on it",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "fs.list_dir", "arguments": '{"path": "C:\\\\"}'},
            }
        ],
    }
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "one file"}


def test_an_image_in_a_tool_result_is_refused_rather_than_dropped() -> None:
    """Chat Completions takes text only there; silently dropping it answers nothing."""
    req = a_request(
        messages=(
            ToolResultMessage(
                tool_call_id="c1", content=(ImagePart(media_type="image/png", data="QUJD"),)
            ),
        )
    )

    with pytest.raises(ProviderCapabilityError):
        wire_body(req, TEST_CONFIG)


def test_tools_and_options_are_only_sent_when_asked_for() -> None:
    bare = wire_body(a_request(), TEST_CONFIG)

    assert "tools" not in bare
    assert "tool_choice" not in bare
    assert "temperature" not in bare
    assert "max_tokens" not in bare
    assert "response_format" not in bare
    assert "stop" not in bare

    full = wire_body(
        a_request(
            tools=(A_TOOL,),
            tool_choice="required",
            temperature=0.2,
            max_output_tokens=256,
            json_mode=True,
            stop=("STOP",),
        ),
        TEST_CONFIG,
    )

    assert full["tools"][0]["function"]["name"] == "fs.list_dir"
    assert full["tool_choice"] == "required"
    assert full["temperature"] == 0.2
    assert full["max_tokens"] == 256
    assert full["response_format"] == {"type": "json_object"}
    assert full["stop"] == ["STOP"]


def test_the_max_tokens_field_is_configurable() -> None:
    """The o-series and some gateways spell it `max_completion_tokens` (`P1-05`)."""
    config = ProviderConfig(
        id="custom",
        base_url="https://gateway.invalid/v1",
        models=TEST_MODELS,
        max_tokens_field="max_completion_tokens",
    )

    body = wire_body(a_request(max_output_tokens=64), config)

    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body


# ---------------------------------------------------------------------------
# Capabilities and the capability gate
# ---------------------------------------------------------------------------


def test_capabilities_are_per_model_and_an_unknown_one_raises() -> None:
    provider = OpenAIProvider(lambda: A_KEY, config=TEST_CONFIG)

    assert provider.capabilities(VISION_MODEL).vision is True
    with pytest.raises(ProviderCapabilityError):
        provider.capabilities("no-such-model")


def test_a_gateway_can_answer_for_models_it_has_no_table_for() -> None:
    """`openrouter` and `custom` serve models we cannot enumerate (`P1-05`)."""
    unknown = Capabilities(vision=False, tool_calling=True, json_mode=False, ctx_window=4_096)
    config = ProviderConfig(
        id="custom", base_url="https://gateway.invalid/v1", models={}, unknown_model=unknown
    )

    caps = OpenAIProvider(lambda: A_KEY, config=config).capabilities("whatever-7b")

    assert caps == unknown
    # An unknown price is unknown, never free.
    assert caps.cost_per_mtok_input is None


def test_the_shipped_openai_table_prices_every_model_it_lists() -> None:
    assert OPENAI.base_url == "https://api.openai.com/v1"
    for model, caps in OPENAI.models.items():
        assert caps.cost_per_mtok_input is not None, model
        assert caps.cost_per_mtok_output is not None, model
        assert caps.cost_per_mtok_output >= caps.cost_per_mtok_input, model


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"tools": (A_TOOL,)}, "cannot call tools"),
        ({"json_mode": True}, "has no JSON mode"),
        (
            {
                "messages": (
                    UserMessage(content=(ImagePart(media_type="image/webp", data="QUJD"),)),
                )
            },
            "cannot see images",
        ),
    ],
)
async def test_the_capability_gate_refuses_before_any_request(
    overrides: dict[str, Any], why: str
) -> None:
    recorder = Recorder()
    provider = provider_over(recorder)

    with pytest.raises(ProviderCapabilityError, match=why):
        await collect(provider.chat(a_request(model="text-only", **overrides)))

    assert recorder.requests == []


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_a_streamed_answer_arrives_as_text_then_usage_then_done() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("Hel"), text_chunk("lo"), finish_chunk(), usage_chunk(10, 4))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["text", "text", "usage", "done"]
    assert "".join(d.text for d in deltas if d.type == "text") == "Hello"
    assert sum(d.type == "done" for d in deltas) == 1
    await provider.aclose()


async def test_usage_carries_a_cost_worked_out_from_the_model_table() -> None:
    recorder = Recorder()
    recorder.stream(finish_chunk(), usage_chunk(1_000_000, 1_000_000))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    assert usage.input_tokens == 1_000_000
    # 0.15 + 0.60 US dollars, in cents.
    assert usage.cost_cents == pytest.approx(75.0)
    await provider.aclose()


async def test_a_tool_call_is_emitted_once_whole_never_in_fragments() -> None:
    recorder = Recorder()
    recorder.stream(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "fs.list_dir", "arguments": '{"pa'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'th": "C:\\\\"}'}}]
                    },
                }
            ]
        },
        finish_chunk("tool_calls"),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["tool_call", "done"]
    call = next(d for d in deltas if isinstance(d, ToolCallDelta)).call
    assert call == ToolCall(id="call_1", name="fs.list_dir", arguments={"path": "C:\\"})
    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "tool_calls"
    await provider.aclose()


async def test_the_key_is_sent_as_a_bearer_header_and_read_every_call() -> None:
    recorder = Recorder()
    recorder.stream(finish_chunk())
    reads = 0

    def lookup() -> str:
        nonlocal reads
        reads += 1
        return A_KEY

    provider = OpenAIProvider(lookup, config=TEST_CONFIG, transport=recorder.transport())

    await collect(provider.chat(a_request()))
    await collect(provider.chat(a_request()))

    assert recorder.requests[0].headers["authorization"] == f"Bearer {A_KEY}"
    assert reads == 2
    # Nothing that outlives a call holds a key (`ARCHITECTURE.md § 5.3`).
    assert A_KEY not in repr(vars(provider))
    await provider.aclose()


async def test_no_key_is_an_auth_error_and_never_reaches_the_wire() -> None:
    recorder = Recorder()
    recorder.stream(finish_chunk())
    provider = provider_over(recorder, key=None)

    with pytest.raises(ProviderAuthError):
        await collect(provider.chat(a_request()))

    assert recorder.requests == []


async def test_abandoning_a_stream_releases_the_connection() -> None:
    """Preemption and a budget breach both walk away from a stream mid-flight."""
    recorder = Recorder()
    recorder.stream(text_chunk("one"), text_chunk("two"), finish_chunk())
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
        (503, ProviderTransientError, True),
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


async def test_a_timeout_is_transient_so_the_router_falls_back() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    provider = OpenAIProvider(
        lambda: A_KEY, config=TEST_CONFIG, transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(ProviderTransientError) as caught:
        await collect(provider.chat(a_request()))

    assert caught.value.retryable is True
    await provider.aclose()


async def test_a_connection_that_dies_mid_answer_is_transient() -> None:
    class BrokenStream(RecordedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"data: " + json.dumps(text_chunk("hi")).encode() + b"\n\n"
            raise httpx2.ReadError("connection reset")

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream([])
        )

    provider = OpenAIProvider(
        lambda: A_KEY, config=TEST_CONFIG, transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(ProviderTransientError):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_the_reader_handles_the_sse_format_a_provider_really_sends() -> None:
    """Events split across reads, keep-alive comments, CRLF, and no trailing blank."""
    first = json.dumps(text_chunk("split "))
    second = json.dumps(text_chunk("across reads"))
    recorder = Recorder()
    recorder.raw(
        [
            b": keep-alive\n\n",
            f"data: {first}".encode(),
            b"\n\r\n",
            f"event: message\r\ndata: {second}\r\n\r\n".encode(),
            f"data: {json.dumps(finish_chunk())}\n\n".encode(),
            # No blank line after the last event, and no `[DONE]` either.
            f"data: {json.dumps(usage_chunk(2, 1))}".encode(),
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
    recorder.stream(finish_chunk(), content_type="application/json")
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_a_stream_without_a_finish_reason_is_a_protocol_error() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("half an answer"))
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError, match="finish reason"):
        await collect(provider.chat(a_request()))

    await provider.aclose()


# ---------------------------------------------------------------------------
# The stream reader on its own
# ---------------------------------------------------------------------------


def test_the_reader_refuses_what_it_cannot_read() -> None:
    with pytest.raises(ProviderProtocolError, match="not JSON"):
        ChatStream("openai").read("{not json")

    with pytest.raises(ProviderProtocolError, match="not an object"):
        ChatStream("openai").read("[]")

    with pytest.raises(ProviderProtocolError, match="unknown finish reason"):
        ChatStream("openai").read(json.dumps(finish_chunk("exploded")))


def test_tool_arguments_that_never_parse_are_refused_not_guessed() -> None:
    stream = ChatStream("openai")
    stream.read(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "t", "arguments": "{oops"},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )

    with pytest.raises(ProviderProtocolError, match="not JSON"):
        stream.flush_tool_calls()


def test_a_tool_call_with_no_name_is_refused() -> None:
    stream = ChatStream("openai")
    stream.read(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1"}]}}]}))

    with pytest.raises(ProviderProtocolError, match="no name or id"):
        stream.flush_tool_calls()


def test_the_tool_call_buffer_is_bounded() -> None:
    """`REVIEW.md § 2`: no unbounded buffer, not even one a model fills."""
    stream = ChatStream("openai")
    fragment = "x" * 4096
    with pytest.raises(ProviderProtocolError, match="too large"):
        for _ in range((MAX_TOOL_ARGUMENT_CHARS // len(fragment)) + 2):
            stream.read(
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": fragment}}
                                    ]
                                }
                            }
                        ]
                    }
                )
            )

    crowd = ChatStream("openai")
    with pytest.raises(ProviderProtocolError, match="too many tools"):
        for index in range(MAX_TOOL_CALLS + 1):
            crowd.read(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": index}]}}]}))


def test_an_unknown_price_costs_an_unknown_amount() -> None:
    known = TEST_MODELS[VISION_MODEL]
    unknown = TEST_MODELS["text-only"]
    usage = Usage(input_tokens=1_000, output_tokens=1_000)

    assert cost_cents(known, usage) == pytest.approx(0.075)
    assert cost_cents(unknown, usage) is None


# ---------------------------------------------------------------------------
# validate_key
# ---------------------------------------------------------------------------


async def test_validate_key_answers_the_test_button_without_raising() -> None:
    recorder = Recorder()
    recorder.status(200, body='{"data": []}')
    provider = provider_over(recorder)

    assert (await provider.validate_key()).valid is True

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

    provider = OpenAIProvider(
        lambda: A_KEY, config=TEST_CONFIG, transport=httpx2.MockTransport(handler)
    )

    assert (await provider.validate_key()).valid is False
    await provider.aclose()


# ---------------------------------------------------------------------------
# Live smoke test (`REVIEW.md § 6`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AEGIS_OPENAI_TEST_KEY"),
    reason="set AEGIS_OPENAI_TEST_KEY to run the live smoke test",
)
async def test_a_live_call_streams_an_answer() -> None:
    provider = OpenAIProvider(lambda: os.environ.get("AEGIS_OPENAI_TEST_KEY"))
    try:
        assert (await provider.validate_key()).valid is True

        deltas = await collect(
            provider.chat(
                ChatRequest(
                    model="gpt-4o-mini",
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
