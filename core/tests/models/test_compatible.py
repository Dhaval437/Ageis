"""`nvidia`, `openrouter` and `custom` — the configs that reuse the `openai` client.

`P1-05`'s whole claim is that these three providers need no code of their own, so the
tests here are of two kinds: the configs themselves, and a real streamed call driven
through `OpenAIProvider` over each of them, proving the client was not forked. The
recorded-response scaffolding is `test_openai`'s, reused for the same reason `common.py`
exists — a second copy is one that drifts.
"""

from __future__ import annotations

import pytest
from aegis_core.models.providers.common import cost_cents
from aegis_core.models.providers.compatible import (
    COMPATIBLE_UNKNOWN,
    MAX_BASE_URL_CHARS,
    NVIDIA,
    OPENROUTER,
    custom_config,
    normalise_base_url,
)
from aegis_core.models.providers.openai import OpenAIProvider, ProviderConfig
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
    A_KEY,
    Recorder,
    collect,
    finish_chunk,
    text_chunk,
    usage_chunk,
)

A_MODEL = "some-vendor/some-model-70b"


def a_request(model: str = A_MODEL) -> ChatRequest:
    return ChatRequest(model=model, messages=(UserMessage(content=(TextPart(text="hello"),)),))


def provider_over(recorder: Recorder, config: ProviderConfig) -> OpenAIProvider:
    return OpenAIProvider(lambda: A_KEY, config=config, transport=recorder.transport())


# ---------------------------------------------------------------------------
# The configs
# ---------------------------------------------------------------------------


def test_nvidia_is_the_documented_endpoint() -> None:
    assert NVIDIA.id == "nvidia"
    assert NVIDIA.base_url == "https://integrate.api.nvidia.com/v1"


def test_openrouter_is_the_documented_endpoint() -> None:
    assert OPENROUTER.id == "openrouter"
    assert OPENROUTER.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("config", [NVIDIA, OPENROUTER, custom_config("https://gw.invalid/v1")])
def test_a_gateway_answers_for_a_model_it_cannot_enumerate(config: ProviderConfig) -> None:
    """Refusing a model the user's key really serves is worse than a provider 404."""
    caps = OpenAIProvider(lambda: A_KEY, config=config).capabilities(A_MODEL)

    assert caps == COMPATIBLE_UNKNOWN
    assert caps.vision and caps.tool_calling and caps.json_mode


@pytest.mark.parametrize("config", [NVIDIA, OPENROUTER, custom_config("https://gw.invalid/v1")])
def test_an_unknown_model_has_no_price_rather_than_a_guessed_one(config: ProviderConfig) -> None:
    """`provider.py`: never guess a price. An unknown price is unknown, never free."""
    caps = OpenAIProvider(lambda: A_KEY, config=config).capabilities(A_MODEL)

    assert caps.cost_per_mtok_input is None
    assert caps.cost_per_mtok_output is None
    assert cost_cents(caps, Usage(input_tokens=1_000_000, output_tokens=1_000_000)) is None


def test_the_unknown_context_window_is_a_conservative_one() -> None:
    """A window is a budget P4-04 fills: guessing high costs a 400 mid-task."""
    assert 0 < COMPATIBLE_UNKNOWN.ctx_window <= 32_768


# ---------------------------------------------------------------------------
# A user-entered base URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entered", "expected"),
    [
        ("https://gw.invalid/v1", "https://gw.invalid/v1"),
        ("  https://gw.invalid/v1  ", "https://gw.invalid/v1"),
        ("https://gw.invalid/v1/", "https://gw.invalid/v1"),
        ("https://gw.invalid/", "https://gw.invalid"),
        ("HTTPS://GW.invalid/v1", "https://gw.invalid/v1"),
        ("https://gw.invalid:8443/openai/v1", "https://gw.invalid:8443/openai/v1"),
        # A local gateway is served over plaintext and has no wire to speak of.
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
        ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
    ],
)
def test_a_usable_address_is_normalised(entered: str, expected: str) -> None:
    assert normalise_base_url(entered) == expected


@pytest.mark.parametrize(
    "entered",
    [
        "",
        "   ",
        "api.example.com/v1",  # no scheme at all
        "ftp://gw.invalid/v1",
        "file:///C:/Windows/System32",
        "gopher://gw.invalid",
        "https:///v1",  # no host
        "https://gw.invalid/v1?key=sk-secret",
        "https://gw.invalid/v1#frag",
        "https://sk-secret@gw.invalid/v1",
        "https://user:sk-secret@gw.invalid/v1",
        "https://gw.invalid:notaport/v1",
        # Plaintext to another machine would put the key on the wire in the clear.
        "http://gw.invalid/v1",
        "http://192.168.1.10:8000/v1",
    ],
)
def test_an_unusable_address_is_refused(entered: str) -> None:
    with pytest.raises(ValueError, match=r"\S"):
        normalise_base_url(entered)


def test_an_absurdly_long_address_is_refused() -> None:
    with pytest.raises(ValueError, match="too long"):
        normalise_base_url("https://gw.invalid/" + "a" * MAX_BASE_URL_CHARS)


def test_a_refusal_is_written_for_the_user_and_quotes_nothing_back() -> None:
    """The address may hold what the user thought was a key. It never comes back."""
    with pytest.raises(ValueError) as caught:
        normalise_base_url("https://gw.invalid/v1?key=sk-secret-0123456789")

    assert "sk-secret-0123456789" not in str(caught.value)


def test_custom_config_carries_the_normalised_address() -> None:
    config = custom_config("https://gw.invalid/v1/")

    assert config.id == "custom"
    assert config.base_url == "https://gw.invalid/v1"
    assert config.unknown_model == COMPATIBLE_UNKNOWN


# ---------------------------------------------------------------------------
# The same client, three endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "url"),
    [
        (NVIDIA, "https://integrate.api.nvidia.com/v1/chat/completions"),
        (OPENROUTER, "https://openrouter.ai/api/v1/chat/completions"),
        (custom_config("http://localhost:1234/v1"), "http://localhost:1234/v1/chat/completions"),
    ],
)
@pytest.mark.asyncio
async def test_a_call_streams_from_the_configured_endpoint(
    config: ProviderConfig, url: str
) -> None:
    recorder = Recorder()
    recorder.stream(text_chunk("hi"), finish_chunk(), usage_chunk(10, 4))
    provider = provider_over(recorder, config)

    deltas = await collect(provider.chat(a_request()))

    assert str(recorder.requests[0].url) == url
    assert recorder.requests[0].headers["authorization"] == f"Bearer {A_KEY}"
    assert [delta for delta in deltas if isinstance(delta, TextDelta)] == [TextDelta(text="hi")]
    assert isinstance(deltas[-1], DoneDelta)


@pytest.mark.asyncio
async def test_a_gateway_call_reports_tokens_but_no_cost() -> None:
    """`P1-09` must see the tokens it cannot price, not a zero."""
    recorder = Recorder()
    recorder.stream(text_chunk("hi"), finish_chunk(), usage_chunk(1000, 500))

    deltas = await collect(provider_over(recorder, OPENROUTER).chat(a_request()))

    usage = next(delta.usage for delta in deltas if isinstance(delta, UsageDelta))
    assert (usage.input_tokens, usage.output_tokens) == (1000, 500)
    assert usage.cost_cents is None


@pytest.mark.asyncio
async def test_tools_and_images_reach_a_gateway_rather_than_being_gated_off() -> None:
    """The permissive unknown capabilities are what make this request legal at all."""
    recorder = Recorder()
    recorder.stream(finish_chunk("tool_calls"))
    req = ChatRequest(
        model=A_MODEL,
        messages=(
            UserMessage(
                content=(
                    TextPart(text="what is this?"),
                    ImagePart(media_type="image/webp", data="QUJD"),
                )
            ),
        ),
        tools=(ToolDef(name="fs.list_dir", description="List a folder.", parameters={}),),
        json_mode=True,
    )

    await collect(provider_over(recorder, NVIDIA).chat(req))

    body = recorder.requests[0].content.decode()
    assert "fs.list_dir" in body
    assert "data:image/webp;base64,QUJD" in body


# ---------------------------------------------------------------------------
# The Test button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_validates_a_key_against_an_authenticated_endpoint() -> None:
    """Its `/models` is public, so `/models` would call any string a working key."""
    recorder = Recorder()
    recorder.status(200)

    status = await provider_over(recorder, OPENROUTER).validate_key()

    assert status.valid
    assert recorder.requests[0].url.path == "/api/v1/key"


@pytest.mark.asyncio
async def test_a_gateway_key_check_still_falls_back_to_models() -> None:
    recorder = Recorder()
    recorder.status(401)

    status = await provider_over(recorder, custom_config("https://gw.invalid/v1")).validate_key()

    assert not status.valid
    assert recorder.requests[0].url.path == "/v1/models"
