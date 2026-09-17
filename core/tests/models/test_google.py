"""The Google Gemini adapter (`P1-04`, `ARCHITECTURE.md § 5.1`).

`REVIEW.md § 6` asks model adapters for recorded-response tests plus one live smoke test
skipped without a key. The recordings here are served by an `httpx2.MockTransport`, so
the whole wire format — `user`/`model` roles, tool results addressed by name, whole
function calls, thought parts that must not become the answer, usage restated on every
chunk — is exercised with no network at all.
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
from aegis_core.models.providers.common import MAX_TOOL_CALLS
from aegis_core.models.providers.google import (
    GOOGLE_MODELS,
    GOOGLE_UNKNOWN,
    GenerateStream,
    GoogleProvider,
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
    UsageDelta,
    UserMessage,
)

A_KEY = "AIza-test-key-do-not-use-0123456789"

MODEL = "gemini-2.5-flash"
TEXT_ONLY = "text-only"
TEST_MODELS = {
    MODEL: GOOGLE_MODELS[MODEL],
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


def sse(*chunks: object) -> list[bytes]:
    """Render chunks the way `?alt=sse` does."""
    return [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]


def candidate(*parts: object, finish: str | None = None, usage: object = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}}
    if finish is not None:
        entry["finishReason"] = finish
    chunk: dict[str, Any] = {"candidates": [entry]}
    if usage is not None:
        chunk["usageMetadata"] = usage
    return chunk


def text_chunk(text: str, **kwargs: Any) -> dict[str, Any]:
    return candidate({"text": text}, **kwargs)


def call_part(name: str = "fs.list_dir", **args: Any) -> dict[str, Any]:
    return {"functionCall": {"name": name, "args": args}}


def usage_meta(prompt: int, completion: int, **extra: int) -> dict[str, Any]:
    return {"promptTokenCount": prompt, "candidatesTokenCount": completion, **extra}


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


def provider_over(recorder: Recorder, *, key: str | None = A_KEY) -> GoogleProvider:
    return GoogleProvider(
        lambda: key,
        base_url="https://gemini.example.invalid/v1beta",
        models=TEST_MODELS,
        transport=recorder.transport(),
    )


async def collect(stream: AsyncIterator[ChatDelta]) -> list[ChatDelta]:
    return [delta async for delta in stream]


# ---------------------------------------------------------------------------
# Request → wire
# ---------------------------------------------------------------------------


def test_a_text_request_becomes_contents() -> None:
    body = wire_body(a_request())

    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert "systemInstruction" not in body
    assert "generationConfig" not in body


def test_the_system_prompt_is_lifted_out_of_the_conversation() -> None:
    body = wire_body(
        a_request(
            messages=(
                SystemMessage(content="be brief"),
                UserMessage(content=(TextPart(text="go"),)),
                SystemMessage(content="and polite"),
            )
        )
    )

    assert body["systemInstruction"] == {"parts": [{"text": "be brief\n\nand polite"}]}
    assert [c["role"] for c in body["contents"]] == ["user"]


def test_the_assistant_is_called_model() -> None:
    """Gemini has two roles, and `assistant` is not one of them."""
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(content=(TextPart(text="on it"),)),
            )
        )
    )

    assert [c["role"] for c in body["contents"]] == ["user", "model"]


def test_an_image_becomes_inline_data() -> None:
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

    assert body["contents"][0]["parts"][1] == {
        "inlineData": {"mimeType": "image/webp", "data": "QUJD"}
    }


def test_a_tool_call_becomes_a_function_call_part_with_object_args() -> None:
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(
                    tool_calls=(
                        ToolCall(id="call_1", name="fs.list_dir", arguments={"path": "C:\\"}),
                    )
                ),
            )
        )
    )

    assert body["contents"][1]["parts"] == [
        {"functionCall": {"name": "fs.list_dir", "args": {"path": "C:\\"}}}
    ]


def test_a_tool_result_is_addressed_by_name_not_by_id() -> None:
    """Gemini has no `tool_call_id`; the name comes from the call being answered."""
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(
                    tool_calls=(ToolCall(id="call_1", name="fs.list_dir", arguments={}),)
                ),
                ToolResultMessage(tool_call_id="call_1", content=(TextPart(text="one file"),)),
            )
        )
    )

    assert body["contents"][2] == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "fs.list_dir", "response": {"output": "one file"}}}
        ],
    }


def test_a_tool_result_for_a_call_that_is_not_here_is_refused_not_guessed() -> None:
    """Sending it under a guessed name would answer the wrong call."""
    with pytest.raises(ProviderProtocolError, match="not in this conversation"):
        wire_body(
            a_request(
                messages=(
                    ToolResultMessage(tool_call_id="call_ghost", content=(TextPart(text="?"),)),
                )
            )
        )


def test_a_failed_tool_says_so_on_the_wire() -> None:
    body = wire_body(
        a_request(
            messages=(
                AssistantMessage(
                    tool_calls=(ToolCall(id="call_1", name="fs.list_dir", arguments={}),)
                ),
                ToolResultMessage(
                    tool_call_id="call_1",
                    content=(TextPart(text="access denied"),),
                    is_error=True,
                ),
            )
        )
    )

    response = body["contents"][1]["parts"][0]["functionResponse"]["response"]
    assert response == {"error": "access denied"}


def test_an_image_in_a_tool_result_rides_along_rather_than_being_dropped() -> None:
    """A `functionResponse` is a JSON object and cannot hold image bytes."""
    body = wire_body(
        a_request(
            messages=(
                AssistantMessage(
                    tool_calls=(ToolCall(id="call_1", name="screen.observe", arguments={}),)
                ),
                ToolResultMessage(
                    tool_call_id="call_1",
                    content=(
                        TextPart(text="here it is"),
                        ImagePart(media_type="image/png", data="QUJD"),
                    ),
                ),
            )
        )
    )

    parts = body["contents"][1]["parts"]
    assert parts[0]["functionResponse"]["response"] == {"output": "here it is"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}


def test_adjacent_same_role_turns_are_merged() -> None:
    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(
                    tool_calls=(ToolCall(id="call_1", name="fs.list_dir", arguments={}),)
                ),
                ToolResultMessage(tool_call_id="call_1", content=(TextPart(text="done"),)),
                UserMessage(content=(TextPart(text="thanks"),)),
            )
        )
    )

    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert len(body["contents"][2]["parts"]) == 2


def test_tools_and_options_are_only_sent_when_asked_for() -> None:
    bare = wire_body(a_request())

    assert "tools" not in bare
    assert "toolConfig" not in bare

    full = wire_body(
        a_request(
            tools=(A_TOOL,),
            tool_choice="required",
            temperature=0.2,
            max_output_tokens=256,
            json_mode=True,
            stop=("STOP",),
        )
    )

    assert full["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "fs.list_dir",
                    "description": "List a folder.",
                    "parameters": A_TOOL.parameters,
                }
            ]
        }
    ]
    # `required` is spelt `ANY` here.
    assert full["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert full["generationConfig"] == {
        "temperature": 0.2,
        "maxOutputTokens": 256,
        "responseMimeType": "application/json",
        "stopSequences": ["STOP"],
    }


@pytest.mark.parametrize(
    ("choice", "mode"),
    [("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")],
)
def test_every_tool_choice_has_a_spelling(choice: str, mode: str) -> None:
    body = wire_body(a_request(tools=(A_TOOL,), tool_choice=choice))

    assert body["toolConfig"]["functionCallingConfig"]["mode"] == mode


# ---------------------------------------------------------------------------
# Capabilities and the capability gate
# ---------------------------------------------------------------------------


def test_a_model_this_build_has_not_heard_of_still_works_but_has_no_price() -> None:
    caps = GoogleProvider(lambda: A_KEY).capabilities("gemini-something-newer")

    assert caps == GOOGLE_UNKNOWN
    assert caps.vision is True
    # An unknown price is unknown, never free.
    assert caps.cost_per_mtok_input is None


def test_the_shipped_table_prices_every_model_it_lists() -> None:
    for model, caps in GOOGLE_MODELS.items():
        assert caps.cost_per_mtok_input is not None, model
        assert caps.cost_per_mtok_output is not None, model
        assert caps.cost_per_mtok_output >= caps.cost_per_mtok_input, model
        # Unlike Anthropic, this API really does have one.
        assert caps.json_mode is True, model


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
        await collect(provider.chat(a_request(model=TEXT_ONLY, **overrides)))

    assert recorder.requests == []


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_a_streamed_answer_arrives_as_text_then_usage_then_done() -> None:
    recorder = Recorder()
    recorder.stream(
        text_chunk("Hel"),
        text_chunk("lo", finish="STOP", usage=usage_meta(10, 4)),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["text", "text", "usage", "done"]
    assert "".join(d.text for d in deltas if d.type == "text") == "Hello"
    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "stop"
    await provider.aclose()


async def test_the_request_names_the_model_in_the_path_and_asks_for_sse() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("hi", finish="STOP"))
    provider = provider_over(recorder)

    await collect(provider.chat(a_request()))

    url = recorder.requests[0].url
    assert url.path.endswith(f"/models/{MODEL}:streamGenerateContent")
    assert url.params["alt"] == "sse"
    await provider.aclose()


async def test_a_model_id_cannot_smuggle_a_path_segment() -> None:
    """The id comes from settings the user typed, so it is escaped, not trusted."""
    recorder = Recorder()
    recorder.stream(text_chunk("hi", finish="STOP"))
    provider = provider_over(recorder)

    await collect(provider.chat(a_request(model="../../models/other:generateContent?x=y")))

    # `url.path` is the *decoded* view; what went on the wire is `raw_path`.
    raw = recorder.requests[0].url.raw_path.decode().partition("?")[0]
    assert raw.endswith(":streamGenerateContent")
    assert raw.count("/models/") == 1
    assert "%2F" in raw
    await provider.aclose()


async def test_usage_carries_a_cost_worked_out_from_the_model_table() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("x", finish="STOP", usage=usage_meta(1_000_000, 1_000_000)))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    # 0.30 + 2.50 US dollars, in cents.
    assert usage.cost_cents == pytest.approx(280.0)
    await provider.aclose()


async def test_thinking_tokens_are_counted_as_output_rather_than_dropped() -> None:
    """They are billed as output and are not in `candidatesTokenCount`."""
    recorder = Recorder()
    recorder.stream(
        text_chunk("x", finish="STOP", usage=usage_meta(10, 20, thoughtsTokenCount=100))
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    assert usage.output_tokens == 120
    await provider.aclose()


async def test_usage_restated_on_every_chunk_is_not_added_up() -> None:
    recorder = Recorder()
    recorder.stream(
        text_chunk("one", usage=usage_meta(10, 1)),
        text_chunk("two", usage=usage_meta(10, 2)),
        text_chunk("three", finish="STOP", usage=usage_meta(10, 3)),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    usage = next(d for d in deltas if isinstance(d, UsageDelta)).usage
    assert usage.input_tokens == 10
    assert usage.output_tokens == 3
    await provider.aclose()


async def test_a_thought_part_never_becomes_the_answer() -> None:
    """A 2.5-series model streams its reasoning as text flagged `thought`."""
    recorder = Recorder()
    recorder.stream(
        candidate({"text": "the user probably wants...", "thought": True}),
        candidate({"text": "Here you go."}, finish="STOP"),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert "".join(d.text for d in deltas if d.type == "text") == "Here you go."
    await provider.aclose()


async def test_a_tool_call_arrives_whole_and_gets_an_id_of_our_own() -> None:
    """Gemini sends `args` as an object and has no id for a call."""
    recorder = Recorder()
    recorder.stream(candidate(call_part(path="C:\\"), finish="STOP"))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    assert [d.type for d in deltas] == ["tool_call", "done"]
    call = next(d for d in deltas if isinstance(d, ToolCallDelta)).call
    assert call.name == "fs.list_dir"
    assert call.arguments == {"path": "C:\\"}
    assert call.id
    await provider.aclose()


async def test_a_tool_call_turn_finishes_tool_calls_even_though_gemini_says_stop() -> None:
    """There is no tool-call finish reason here, so the answer decides."""
    recorder = Recorder()
    recorder.stream(candidate({"text": "let me look"}, call_part(path="C:\\"), finish="STOP"))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "tool_calls"
    await provider.aclose()


async def test_a_truncated_tool_call_turn_still_reports_length() -> None:
    """`length` says the turn was cut short, so it outranks the tool calls in it."""
    recorder = Recorder()
    recorder.stream(candidate(call_part(path="C:\\"), finish="MAX_TOKENS"))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "length"
    await provider.aclose()


async def test_the_ids_minted_for_parallel_calls_are_distinct() -> None:
    recorder = Recorder()
    recorder.stream(
        candidate(call_part("first", a=1), call_part("second", b=2), finish="STOP"),
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))

    calls = [d.call for d in deltas if isinstance(d, ToolCallDelta)]
    assert [c.name for c in calls] == ["first", "second"]
    assert len({c.id for c in calls}) == 2
    await provider.aclose()


async def test_a_minted_id_survives_the_round_trip_back_to_a_name() -> None:
    """The id this adapter invented must resolve to the same name a turn later."""
    recorder = Recorder()
    recorder.stream(candidate(call_part(path="C:\\"), finish="STOP"))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request(tools=(A_TOOL,))))
    call = next(d for d in deltas if isinstance(d, ToolCallDelta)).call

    body = wire_body(
        a_request(
            messages=(
                UserMessage(content=(TextPart(text="go"),)),
                AssistantMessage(tool_calls=(call,)),
                ToolResultMessage(tool_call_id=call.id, content=(TextPart(text="one file"),)),
            )
        )
    )

    assert body["contents"][2]["parts"][0]["functionResponse"]["name"] == "fs.list_dir"
    await provider.aclose()


@pytest.mark.parametrize(
    ("raw", "finish_reason"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("PROHIBITED_CONTENT", "content_filter"),
        ("SPII", "content_filter"),
        ("BLOCKLIST", "content_filter"),
    ],
)
async def test_every_finish_reason_maps(raw: str, finish_reason: str) -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("x", finish=raw))
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    done = deltas[-1]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == finish_reason
    await provider.aclose()


async def test_a_prompt_blocked_before_generation_is_a_refusal_not_an_error() -> None:
    """A refusal is a successful call that finishes `content_filter`."""
    recorder = Recorder()
    recorder.stream({"promptFeedback": {"blockReason": "SAFETY"}})
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert [d.type for d in deltas] == ["done"]
    done = deltas[0]
    assert isinstance(done, DoneDelta)
    assert done.finish_reason == "content_filter"
    await provider.aclose()


async def test_the_key_is_sent_as_a_google_header_and_read_every_call() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("x", finish="STOP"))
    reads = 0

    def lookup() -> str:
        nonlocal reads
        reads += 1
        return A_KEY

    provider = GoogleProvider(
        lookup,
        base_url="https://gemini.example.invalid/v1beta",
        models=TEST_MODELS,
        transport=recorder.transport(),
    )

    await collect(provider.chat(a_request()))
    await collect(provider.chat(a_request()))

    assert recorder.requests[0].headers["x-goog-api-key"] == A_KEY
    assert reads == 2
    # Nothing that outlives a call holds a key (`ARCHITECTURE.md § 5.3`).
    assert A_KEY not in repr(vars(provider))
    await provider.aclose()


async def test_the_key_never_reaches_the_url() -> None:
    """A URL carries into access logs, proxy logs and crash reports; a key must not."""
    recorder = Recorder()
    recorder.stream(text_chunk("x", finish="STOP"))
    provider = provider_over(recorder)

    await collect(provider.chat(a_request()))

    assert A_KEY not in str(recorder.requests[0].url)
    assert "key" not in recorder.requests[0].url.params
    await provider.aclose()


async def test_no_key_is_an_auth_error_and_never_reaches_the_wire() -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("x", finish="STOP"))
    provider = provider_over(recorder, key=None)

    with pytest.raises(ProviderAuthError):
        await collect(provider.chat(a_request()))

    assert recorder.requests == []


async def test_abandoning_a_stream_releases_the_connection() -> None:
    """Preemption and a budget breach both walk away from a stream mid-flight."""
    recorder = Recorder()
    recorder.stream(text_chunk("one"), text_chunk("two", finish="STOP"))
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
    recorder.status(400, body=json.dumps({"error": {"message": f"API key invalid: {A_KEY}"}}))
    provider = provider_over(recorder)

    with pytest.raises(ProviderProtocolError) as caught:
        await collect(provider.chat(a_request()))

    assert A_KEY not in str(caught.value)
    assert "API key invalid" not in str(caught.value)
    await provider.aclose()


async def test_a_timeout_is_transient_so_the_router_falls_back() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    provider = GoogleProvider(
        lambda: A_KEY, models=TEST_MODELS, transport=httpx2.MockTransport(handler)
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

    provider = GoogleProvider(
        lambda: A_KEY, models=TEST_MODELS, transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(ProviderTransientError):
        await collect(provider.chat(a_request()))

    await provider.aclose()


async def test_the_reader_handles_the_sse_format_the_api_really_sends() -> None:
    """Events split across reads, CRLF, and no trailing blank line."""
    recorder = Recorder()
    first = json.dumps(text_chunk("split "))
    second = json.dumps(text_chunk("across reads"))
    recorder.raw(
        [
            f"data: {first}".encode(),
            b"\n\r\n",
            f"data: {second}\r\n\r\n".encode(),
            # No blank line after the last event.
            f"data: {json.dumps(text_chunk('.', finish='STOP', usage=usage_meta(2, 1)))}".encode(),
        ]
    )
    provider = provider_over(recorder)

    deltas = await collect(provider.chat(a_request()))

    assert "".join(d.text for d in deltas if d.type == "text") == "split across reads."
    assert deltas[-1].type == "done"
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
    recorder.stream(text_chunk("x", finish="STOP"), content_type="application/json")
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
        GenerateStream().read("{not json")

    with pytest.raises(ProviderProtocolError, match="not an object"):
        GenerateStream().read("[]")

    with pytest.raises(ProviderProtocolError, match="unknown finish reason"):
        GenerateStream().read(json.dumps(text_chunk("x", finish="OTHER")))

    with pytest.raises(ProviderProtocolError, match="mangled"):
        GenerateStream().read(json.dumps(text_chunk("x", finish="MALFORMED_FUNCTION_CALL")))


def test_a_tool_call_with_no_name_is_refused() -> None:
    stream = GenerateStream()

    with pytest.raises(ProviderProtocolError, match="no name"):
        stream.read(json.dumps(candidate({"functionCall": {"args": {}}})))


def test_tool_arguments_that_are_not_an_object_are_refused() -> None:
    stream = GenerateStream()

    with pytest.raises(ProviderProtocolError, match="not an object"):
        stream.read(json.dumps(candidate({"functionCall": {"name": "t", "args": [1, 2]}})))


def test_a_tool_call_with_no_arguments_is_an_empty_object() -> None:
    stream = GenerateStream()
    stream.read(json.dumps(candidate({"functionCall": {"name": "screen.observe"}})))

    assert stream.flush_tool_calls()[0].arguments == {}


def test_the_tool_call_buffer_is_bounded() -> None:
    """`REVIEW.md § 2`: no unbounded buffer, not even one a model fills."""
    stream = GenerateStream()

    with pytest.raises(ProviderProtocolError, match="too many tools"):
        for index in range(MAX_TOOL_CALLS + 1):
            stream.read(json.dumps(candidate(call_part(f"tool_{index}"))))


# ---------------------------------------------------------------------------
# validate_key
# ---------------------------------------------------------------------------


async def test_validate_key_answers_the_test_button_without_raising() -> None:
    recorder = Recorder()
    recorder.status(200, body='{"models": []}')
    provider = provider_over(recorder)

    assert (await provider.validate_key()).valid is True
    assert recorder.requests[-1].url.path.endswith("/models")

    # This API rejects a key with 400, not 401.
    recorder.status(400, body=f"API key not valid: {A_KEY}")
    rejected = await provider.validate_key()
    assert rejected.valid is False
    assert "rejected" in rejected.detail
    assert A_KEY not in rejected.detail

    recorder.status(401)
    assert (await provider.validate_key()).valid is False

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

    provider = GoogleProvider(lambda: A_KEY, transport=httpx2.MockTransport(handler))

    assert (await provider.validate_key()).valid is False
    await provider.aclose()


# ---------------------------------------------------------------------------
# Live smoke test (`REVIEW.md § 6`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AEGIS_GOOGLE_TEST_KEY"),
    reason="set AEGIS_GOOGLE_TEST_KEY to run the live smoke test",
)
async def test_a_live_call_streams_an_answer() -> None:
    provider = GoogleProvider(lambda: os.environ.get("AEGIS_GOOGLE_TEST_KEY"))
    try:
        assert (await provider.validate_key()).valid is True

        deltas = await collect(
            provider.chat(
                ChatRequest(
                    model="gemini-2.5-flash",
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
