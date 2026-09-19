"""The local provider (`P1-06`) — the privacy story, so most of this is about *not*.

Not needing a key, not costing anything, not leaving the machine, and not claiming a
capability the model on disk does not have. The recorded-response scaffolding is
`test_openai`'s, for the same reason `common.py` exists: a second copy is one that
drifts. `test_a_live_call_streams_an_answer` is `REVIEW.md § 6`'s live smoke test, and is
the one place a real Ollama is used — skipped unless one is running.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx2
import pytest
from aegis_core.models.provider import (
    ModelProvider,
    ProviderCapabilityError,
    ProviderTransientError,
)
from aegis_core.models.providers import ollama as ollama_module
from aegis_core.models.providers.common import cost_cents
from aegis_core.models.providers.ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_CTX_WINDOW,
    DEFAULT_PORT,
    MAX_CONCURRENT_DESCRIBES,
    MAX_MODELS,
    OLLAMA_UNKNOWN,
    OllamaProvider,
    default_base_url,
    detect,
)
from aegis_core.models.schemas import (
    ChatRequest,
    DoneDelta,
    ImagePart,
    TextDelta,
    TextPart,
    ToolDef,
    Usage,
    UsageDelta,
    UserMessage,
)

from tests.models.test_openai import (
    Recorder,
    collect,
    finish_chunk,
    text_chunk,
    usage_chunk,
)

A_MODEL = "llama3.1:8b"
A_VISION_MODEL = "llava:7b"


def a_request(model: str = A_MODEL, **overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "model": model,
        "messages": (UserMessage(content=(TextPart(text="hello"),)),),
    }
    return ChatRequest(**(base | overrides))


def tags(*names: str) -> dict[str, Any]:
    return {"models": [{"name": name, "model": name} for name in names]}


def show(*capabilities: str) -> dict[str, Any]:
    return {"capabilities": list(capabilities)}


class Ollama:
    """A recorded local server: `/api/*` from a script, `/v1/*` from a recorder."""

    def __init__(self, *, chat: Recorder | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.tags: dict[str, Any] = tags(A_MODEL)
        self.show: dict[str, dict[str, Any]] = {}
        self.status = 200
        self.offline = False
        #: How long `/api/show` takes to answer, for the discovery-budget tests.
        self.show_delay = 0.0
        #: The most describes the server ever had in flight at once.
        self.peak_in_flight = 0
        self._in_flight = 0
        self._chat = chat

    def transport(self) -> httpx2.MockTransport:
        chat = self._chat.transport() if self._chat is not None else None

        async def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            if self.offline:
                raise httpx2.ConnectError("connection refused", request=request)
            if request.url.path == "/api/tags":
                return httpx2.Response(self.status, json=self.tags)
            if request.url.path == "/api/show":
                self._in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
                try:
                    if self.show_delay:
                        await asyncio.sleep(self.show_delay)
                finally:
                    self._in_flight -= 1
                model = request.content.decode()
                for name, payload in self.show.items():
                    if f'"{name}"' in model:
                        return httpx2.Response(200, json=payload)
                return httpx2.Response(404, json={"error": "not found"})
            if chat is not None:
                return await chat.handle_async_request(request)
            raise AssertionError(f"unexpected path {request.url.path}")

        return httpx2.MockTransport(handler)


def provider_over(server: Ollama, **kwargs: Any) -> OllamaProvider:
    return OllamaProvider(transport=server.transport(), **kwargs)


# ---------------------------------------------------------------------------
# It is a provider, and it needs no key
# ---------------------------------------------------------------------------


def test_it_satisfies_the_provider_protocol() -> None:
    assert isinstance(OllamaProvider(), ModelProvider)
    assert OllamaProvider().id == "ollama"


def test_the_default_address_is_the_v4_loopback_and_needs_no_resolver() -> None:
    """`localhost` resolves to `::1` first on Windows, where Ollama is not listening."""
    assert f"http://127.0.0.1:{DEFAULT_PORT}" == DEFAULT_BASE_URL
    assert OllamaProvider().base_url == DEFAULT_BASE_URL


@pytest.mark.asyncio
async def test_a_call_needs_no_key_at_all() -> None:
    """The whole privacy story: a local model must work with nothing configured."""
    chat = Recorder()
    chat.stream(text_chunk("hi"), finish_chunk())
    server = Ollama(chat=chat)
    provider = provider_over(server)

    deltas = await collect(provider.chat(a_request()))
    await provider.aclose()

    assert [d for d in deltas if isinstance(d, TextDelta)] == [TextDelta(text="hi")]
    assert isinstance(deltas[-1], DoneDelta)
    assert "authorization" not in chat.requests[0].headers


@pytest.mark.asyncio
async def test_the_call_goes_to_the_openai_compatible_endpoint_on_this_machine() -> None:
    chat = Recorder()
    chat.stream(finish_chunk())
    server = Ollama(chat=chat)
    provider = provider_over(server)

    await collect(provider.chat(a_request()))
    await provider.aclose()

    assert str(chat.requests[0].url) == f"{DEFAULT_BASE_URL}/v1/chat/completions"


@pytest.mark.asyncio
async def test_a_key_is_still_sent_when_one_is_saved() -> None:
    """A proxy in front of a local server may want one; Ollama itself ignores it."""
    chat = Recorder()
    chat.stream(finish_chunk())
    server = Ollama(chat=chat)
    provider = OllamaProvider(lambda: "a-proxy-key", transport=server.transport())

    await collect(provider.chat(a_request()))
    await provider.aclose()

    assert chat.requests[0].headers["authorization"] == "Bearer a-proxy-key"


# ---------------------------------------------------------------------------
# Cost: free is a fact here, not a guess
# ---------------------------------------------------------------------------


def test_a_local_model_is_priced_at_zero_rather_than_unknown() -> None:
    """`P1-09` must not pause a task that costs nothing."""
    assert OLLAMA_UNKNOWN.cost_per_mtok_input == 0.0
    assert OLLAMA_UNKNOWN.cost_per_mtok_output == 0.0
    assert cost_cents(OLLAMA_UNKNOWN, Usage(input_tokens=10**9, output_tokens=10**9)) == 0.0


@pytest.mark.asyncio
async def test_a_call_reports_tokens_and_a_zero_cost() -> None:
    chat = Recorder()
    chat.stream(text_chunk("hi"), finish_chunk(), usage_chunk(1000, 500))
    provider = provider_over(Ollama(chat=chat))

    deltas = await collect(provider.chat(a_request()))
    await provider.aclose()

    usage = next(d.usage for d in deltas if isinstance(d, UsageDelta))
    assert (usage.input_tokens, usage.output_tokens) == (1000, 500)
    assert usage.cost_cents == 0.0


# ---------------------------------------------------------------------------
# Capabilities come from the server, not from a table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_reads_the_installed_models_and_what_each_one_can_do() -> None:
    server = Ollama()
    server.tags = tags(A_MODEL, A_VISION_MODEL)
    server.show = {
        A_MODEL: show("completion", "tools"),
        A_VISION_MODEL: show("completion", "vision"),
    }
    provider = provider_over(server)

    found = await provider.refresh()
    await provider.aclose()

    assert found == (A_MODEL, A_VISION_MODEL)
    assert provider.capabilities(A_MODEL).tool_calling is True
    assert provider.capabilities(A_MODEL).vision is False
    assert provider.capabilities(A_VISION_MODEL).vision is True
    assert provider.capabilities(A_VISION_MODEL).tool_calling is False


@pytest.mark.asyncio
async def test_a_model_the_server_will_not_describe_falls_back_conservatively() -> None:
    """One odd model must not cost the user every other model on the machine."""
    server = Ollama()
    server.tags = tags(A_MODEL, "weird:latest")
    server.show = {A_MODEL: show("completion", "vision")}
    provider = provider_over(server)

    await provider.refresh()
    await provider.aclose()

    assert provider.capabilities(A_MODEL).vision is True
    assert provider.capabilities("weird:latest") == OLLAMA_UNKNOWN


@pytest.mark.asyncio
async def test_a_model_that_was_never_discovered_falls_back_conservatively() -> None:
    provider = provider_over(Ollama())

    assert provider.capabilities("never-pulled:latest") == OLLAMA_UNKNOWN
    await provider.aclose()


def test_the_unknown_model_refuses_vision_rather_than_answering_blind() -> None:
    """Ollama takes an image for a text model and answers as if it were not there."""
    assert OLLAMA_UNKNOWN.vision is False


def test_the_context_window_is_the_servers_default_not_the_models() -> None:
    """Ollama truncates to `num_ctx` silently; a window guessed high loses the prompt."""
    assert OLLAMA_UNKNOWN.ctx_window == DEFAULT_CTX_WINDOW == 4096


@pytest.mark.asyncio
async def test_an_image_to_a_text_only_model_is_refused_before_the_wire() -> None:
    server = Ollama()
    server.show = {A_MODEL: show("completion", "tools")}
    provider = provider_over(server)
    await provider.refresh()
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

    with pytest.raises(ProviderCapabilityError, match="cannot see images"):
        await collect(provider.chat(req))
    await provider.aclose()


@pytest.mark.asyncio
async def test_tools_reach_a_model_that_reports_them() -> None:
    chat = Recorder()
    chat.stream(finish_chunk("tool_calls"))
    server = Ollama(chat=chat)
    server.show = {A_MODEL: show("completion", "tools")}
    provider = provider_over(server)
    await provider.refresh()

    await collect(
        provider.chat(
            a_request(tools=(ToolDef(name="fs.list_dir", description="List.", parameters={}),))
        )
    )
    await provider.aclose()

    assert "fs.list_dir" in chat.requests[0].content.decode()


@pytest.mark.asyncio
async def test_discovery_asks_about_models_concurrently_but_not_without_a_bound() -> None:
    """Serial asking is what turns a stalled local server into a startup that hangs."""
    server = Ollama()
    names = tuple(f"model-{index}:latest" for index in range(MAX_CONCURRENT_DESCRIBES * 3))
    server.tags = tags(*names)
    server.show = {name: show("completion", "tools") for name in names}
    server.show_delay = 0.02
    provider = provider_over(server)

    found = await provider.refresh()
    await provider.aclose()

    assert len(found) == len(names)
    assert server.peak_in_flight > 1, "describes ran one after another"
    assert server.peak_in_flight <= MAX_CONCURRENT_DESCRIBES


@pytest.mark.asyncio
async def test_a_server_that_stalls_costs_one_budget_not_one_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DETECT_TIMEOUT_S` bounds one call; the whole set needs its own bound."""
    monkeypatch.setattr(ollama_module, "DISCOVERY_BUDGET_S", 0.1)
    server = Ollama()
    names = tuple(f"model-{index}:latest" for index in range(MAX_MODELS))
    server.tags = tags(*names)
    server.show = {name: show("completion", "vision") for name in names}
    server.show_delay = 5.0
    provider = provider_over(server)

    started = asyncio.get_running_loop().time()
    found = await provider.refresh()
    elapsed = asyncio.get_running_loop().time() - started
    await provider.aclose()

    assert elapsed < 5.0, "the budget bounds the set, not each call"
    # Every model is still listed, with the conservative answer rather than none at all.
    assert found == names
    assert provider.capabilities(names[0]) == OLLAMA_UNKNOWN


@pytest.mark.asyncio
async def test_the_model_list_is_bounded() -> None:
    """`REVIEW.md § 2`: nothing in-memory grows without a bound."""
    server = Ollama()
    server.tags = tags(*(f"model-{index}:latest" for index in range(MAX_MODELS + 20)))
    provider = provider_over(server)

    found = await provider.refresh()
    await provider.aclose()

    assert len(found) == MAX_MODELS


# ---------------------------------------------------------------------------
# The Test button asks "are you running?", because there is no key to be wrong
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_running_server_with_models_is_valid() -> None:
    server = Ollama()
    server.tags = tags(A_MODEL, A_VISION_MODEL)
    provider = provider_over(server)

    status = await provider.validate_key()
    await provider.aclose()

    assert status.valid
    assert "2 models" in status.detail


@pytest.mark.asyncio
async def test_a_server_that_is_not_running_says_so_and_names_the_fix() -> None:
    server = Ollama()
    server.offline = True
    provider = provider_over(server)

    status = await provider.validate_key()
    await provider.aclose()

    assert not status.valid
    assert "running" in status.detail


@pytest.mark.asyncio
async def test_a_running_server_with_nothing_pulled_is_not_usable_yet() -> None:
    server = Ollama()
    server.tags = tags()
    provider = provider_over(server)

    status = await provider.validate_key()
    await provider.aclose()

    assert not status.valid
    assert "ollama pull" in status.detail


# ---------------------------------------------------------------------------
# Auto-detect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_returns_a_refreshed_provider_when_ollama_is_running() -> None:
    server = Ollama()
    server.tags = tags(A_MODEL)
    server.show = {A_MODEL: show("completion", "vision", "tools")}

    provider = await detect(transport=server.transport())

    assert provider is not None
    assert provider.models[A_MODEL].vision is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_detect_answers_none_on_a_machine_without_ollama() -> None:
    """The ordinary case. It must not raise, and must not take long."""
    server = Ollama()
    server.offline = True

    assert await detect(transport=server.transport()) is None


@pytest.mark.asyncio
async def test_detect_answers_none_for_an_address_it_cannot_use() -> None:
    """`P1-10` passes a URL the user typed, which is not a programming error."""
    server = Ollama()

    assert await detect(base_url="http://192.168.1.10:11434", transport=server.transport()) is None
    assert server.requests == [], "a refused address must never be contacted"


@pytest.mark.asyncio
async def test_detect_answers_none_when_the_server_errors() -> None:
    server = Ollama()
    server.status = 500

    assert await detect(transport=server.transport()) is None


# ---------------------------------------------------------------------------
# OLLAMA_HOST — the one address the user can point this at
# ---------------------------------------------------------------------------


def test_no_environment_means_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    assert default_base_url() == DEFAULT_BASE_URL


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
        ("  localhost:8080  ", "http://localhost:8080"),
        # Ollama's own tooling defaults the port when the value omits it.
        ("127.0.0.1", "http://127.0.0.1:11434"),
        ("https://ollama.example.com", "https://ollama.example.com:11434"),
        ("https://ollama.example.com:443/proxy", "https://ollama.example.com:443/proxy"),
    ],
)
def test_ollama_host_is_honoured(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", value)

    assert default_base_url() == expected


@pytest.mark.parametrize(
    "value",
    [
        # Plaintext to another machine would put observations of the user's screen on
        # the wire in the clear, which is the opposite of why this provider exists.
        "http://192.168.1.10:11434",
        "ollama.example.com:11434",
        "file:///C:/Windows/System32",
        "http://user:secret@127.0.0.1:11434",
        "http://127.0.0.1:11434?key=secret",
    ],
)
def test_an_unusable_ollama_host_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OLLAMA_HOST", value)

    with pytest.raises(ValueError, match=r"\S"):
        default_base_url()


@pytest.mark.asyncio
async def test_detect_survives_a_misconfigured_ollama_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup must not fail because an environment variable is wrong."""
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.10:11434")

    assert await detect() is None


@pytest.mark.asyncio
async def test_detect_uses_the_address_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
    server = Ollama()

    provider = await detect(transport=server.transport())

    assert provider is not None
    assert provider.base_url == "http://127.0.0.1:9999"
    assert str(server.requests[0].url) == "http://127.0.0.1:9999/api/tags"
    await provider.aclose()


# ---------------------------------------------------------------------------
# Failure is a `ProviderError`, never a raw client exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_raises_the_same_error_a_call_would() -> None:
    server = Ollama()
    server.offline = True
    provider = provider_over(server)

    with pytest.raises(ProviderTransientError):
        await provider.refresh()
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_server_that_does_not_answer_json_is_a_provider_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>a proxy login page</html>")

    provider = OllamaProvider(transport=httpx2.MockTransport(handler))

    with pytest.raises(ProviderTransientError):
        await provider.refresh()
    await provider.aclose()


@pytest.mark.asyncio
async def test_closing_twice_is_safe() -> None:
    provider = provider_over(Ollama())

    await provider.aclose()
    await provider.aclose()


# ---------------------------------------------------------------------------
# Live smoke test (`REVIEW.md § 6`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AEGIS_OLLAMA_TEST_MODEL"),
    reason="set AEGIS_OLLAMA_TEST_MODEL to a pulled model to run the live smoke test",
)
async def test_a_live_call_streams_an_answer() -> None:
    provider = await detect()
    assert provider is not None, "no Ollama is running on this machine"
    try:
        assert (await provider.validate_key()).valid is True

        deltas = await collect(
            provider.chat(
                ChatRequest(
                    model=os.environ["AEGIS_OLLAMA_TEST_MODEL"],
                    messages=(UserMessage(content=(TextPart(text="Say OK and nothing else."),)),),
                    max_output_tokens=8,
                    timeout_s=60,
                )
            )
        )
    finally:
        await provider.aclose()

    assert deltas[-1].type == "done"
    assert "".join(d.text for d in deltas if d.type == "text").strip()
