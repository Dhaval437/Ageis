"""The `ModelProvider` contract and its error hierarchy (`ARCHITECTURE.md § 5.1`)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from aegis_core.models.provider import (
    ModelProvider,
    ProviderAuthError,
    ProviderCapabilityError,
    ProviderError,
    ProviderProtocolError,
    ProviderTransientError,
)
from aegis_core.models.schemas import (
    Capabilities,
    ChatDelta,
    ChatRequest,
    DoneDelta,
    KeyStatus,
    ProviderId,
    TextDelta,
    TextPart,
    UserMessage,
)

A_REQUEST = ChatRequest(model="fake-1", messages=(UserMessage(content=(TextPart(text="hi"),)),))


class FakeProvider:
    """A minimal adapter, written the way a real one is: `chat` is an async generator."""

    id: ProviderId = "custom"

    def __init__(self) -> None:
        self.closed = False

    def capabilities(self, model: str) -> Capabilities:
        if model != "fake-1":
            raise ProviderCapabilityError("custom", f"unknown model {model!r}")
        return Capabilities(vision=True, tool_calling=True, json_mode=False, ctx_window=8192)

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        try:
            yield TextDelta(text="one")
            yield TextDelta(text="two")
            yield DoneDelta(finish_reason="stop")
        finally:
            self.closed = True

    async def validate_key(self) -> KeyStatus:
        return KeyStatus(valid=True, detail="Connected.")


def test_a_minimal_adapter_satisfies_the_protocol() -> None:
    """Structural, both ways: mypy checks the annotation, isinstance checks the object."""
    provider: ModelProvider = FakeProvider()

    assert isinstance(provider, ModelProvider)


def test_an_object_missing_a_member_is_not_a_provider() -> None:
    class NotAProvider:
        id = "custom"

    assert not isinstance(NotAProvider(), ModelProvider)


async def test_a_stream_ends_in_exactly_one_done() -> None:
    deltas = [delta async for delta in FakeProvider().chat(A_REQUEST)]

    assert [d.type for d in deltas] == ["text", "text", "done"]
    assert sum(d.type == "done" for d in deltas) == 1


async def test_abandoning_a_stream_releases_the_connection() -> None:
    """Preemption and a budget breach both abandon a stream mid-flight."""
    provider = FakeProvider()
    stream = provider.chat(A_REQUEST)
    assert isinstance(stream, AsyncGenerator)

    assert (await anext(stream)).type == "text"
    await stream.aclose()

    assert provider.closed is True


def test_capabilities_are_asked_per_model() -> None:
    """A provider serves many models; vision is a property of the model, not the key."""
    provider = FakeProvider()

    assert provider.capabilities("fake-1").vision is True
    with pytest.raises(ProviderCapabilityError):
        provider.capabilities("fake-2")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_type",
    [ProviderTransientError, ProviderAuthError, ProviderCapabilityError, ProviderProtocolError],
)
def test_every_provider_error_is_catchable_as_one(error_type: type[ProviderError]) -> None:
    """The router catches `ProviderError`; nothing else may escape an adapter."""
    with pytest.raises(ProviderError):
        raise error_type("openai", "something went wrong")


def test_only_a_transient_error_triggers_fallback() -> None:
    """`ARCHITECTURE.md § 5.3`: 429/5xx/timeout only — never a refusal, never a bad key."""
    assert ProviderTransientError("openai", "rate limited", status=429).retryable is True

    assert ProviderAuthError("openai", "key rejected", status=401).retryable is False
    assert ProviderCapabilityError("ollama", "no vision").retryable is False
    assert ProviderProtocolError("custom", "unreadable stream").retryable is False


def test_an_error_names_the_provider_and_the_status() -> None:
    error = ProviderTransientError("anthropic", "rate limited", status=429)

    assert str(error) == "anthropic (429): rate limited"
    assert error.provider_id == "anthropic"
    assert error.status == 429


def test_an_error_without_a_status_still_reads() -> None:
    assert str(ProviderTransientError("ollama", "timed out")) == "ollama: timed out"
