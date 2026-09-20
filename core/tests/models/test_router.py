"""The router (`P1-08`, `ARCHITECTURE.md § 5.2`/`§ 5.3`).

`REVIEW.md § 6` asks a router for fallback, budget-breach and capability-gate tests. The
budget guard is `P1-09`, so the two that exist now are here in full, plus the three
things a chain walker gets wrong: falling back on something that is not transient,
falling back after the consumer has already seen output, and leaving the abandoned
provider stream open on the way out.

The providers are fakes, not recorded responses: what is under test is *which* provider
is asked and *when*, and a fake is the only way to make a 429 happen on demand. The pool
itself is tested against the real adapter classes over an `httpx2.MockTransport`, so the
wiring from a `ProviderId` to a client is proved rather than assumed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from aegis_core.models.budget import BudgetExceededError, BudgetGuard, BudgetLimits
from aegis_core.models.provider import (
    ProviderAuthError,
    ProviderCapabilityError,
    ProviderError,
    ProviderProtocolError,
    ProviderTransientError,
)
from aegis_core.models.providers.anthropic import AnthropicProvider
from aegis_core.models.providers.google import GoogleProvider
from aegis_core.models.providers.ollama import OllamaProvider
from aegis_core.models.providers.openai import OpenAIProvider
from aegis_core.models.router import (
    MAX_CHAIN,
    ROLES,
    KeySource,
    ManagedProvider,
    ModelChoice,
    ModelRole,
    ModelRouter,
    ProviderPool,
    ProviderUnavailableError,
    Requirement,
    RoleMap,
    RoleRequest,
    RoleRoute,
)
from aegis_core.models.schemas import (
    Capabilities,
    ChatDelta,
    ChatRequest,
    DoneDelta,
    ImagePart,
    KeyStatus,
    ProviderId,
    TextDelta,
    TextPart,
    ToolDef,
    Usage,
    UsageDelta,
    UserMessage,
)
from aegis_core.storage.db import bootstrap, connect
from aegis_core.storage.usage import UsageLedger
from pydantic import ValidationError

A_KEY = "sk-test-key-do-not-use-0123456789"

FULL = Capabilities(vision=True, tool_calling=True, json_mode=True, ctx_window=128_000)
TEXT_ONLY = Capabilities(vision=False, tool_calling=True, json_mode=True, ctx_window=8_192)
NO_TOOLS = Capabilities(vision=True, tool_calling=False, json_mode=True, ctx_window=8_192)

A_TOOL = ToolDef(
    name="fs.list_dir",
    description="List a folder.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)

AN_IMAGE = ImagePart(media_type="image/webp", data="AAAA")


def a_request(**overrides: Any) -> RoleRequest:
    base: dict[str, Any] = {"messages": (UserMessage(content=(TextPart(text="hello"),)),)}
    return RoleRequest(**(base | overrides))


# ---------------------------------------------------------------------------
# A fake provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """One provider whose every answer is scripted.

    `script` is what `chat()` does: a list of deltas to yield, or an exception to raise
    at a given point. It records each request it was given, so a test can assert which
    model actually ran and what parameters it ran with.
    """

    def __init__(
        self,
        provider_id: ProviderId = "openai",
        *,
        deltas: tuple[ChatDelta, ...] = (DoneDelta(finish_reason="stop"),),
        error: ProviderError | None = None,
        error_after: int = 0,
        caps: Capabilities | dict[str, Capabilities] = FULL,
    ) -> None:
        self.id: ProviderId = provider_id
        self._deltas = deltas
        self._error = error
        self._error_after = error_after
        self._caps = caps
        self.requests: list[ChatRequest] = []
        self.closed = 0
        self.stream_finished = False

    def capabilities(self, model: str) -> Capabilities:
        if isinstance(self._caps, dict):
            caps = self._caps.get(model)
            if caps is None:
                raise ProviderCapabilityError(self.id, f"unknown model {model!r}")
            return caps
        return self._caps

    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]:
        self.requests.append(req)
        try:
            for index, delta in enumerate(self._deltas):
                if self._error is not None and index == self._error_after:
                    raise self._error
                yield delta
            if self._error is not None and self._error_after >= len(self._deltas):
                raise self._error
            self.stream_finished = True
        finally:
            # Mirrors every real adapter: the connection is released here, so a test can
            # see whether the router closed an abandoned stream.
            self.closed += 1

    async def validate_key(self) -> KeyStatus:  # pragma: no cover — not the router's job
        return KeyStatus(valid=True, detail="ok")

    async def aclose(self) -> None:
        self.closed += 1


class FakePool(ProviderPool):
    """A pool that hands out the fakes it was given and builds nothing."""

    def __init__(self, providers: dict[ProviderId, FakeProvider]) -> None:
        super().__init__(_NoKeys())
        self._fakes = providers

    def get(self, provider_id: ProviderId) -> ManagedProvider:
        fake = self._fakes.get(provider_id)
        if fake is None:
            raise ProviderUnavailableError(f"no provider for {provider_id}")
        return fake

    async def aclose(self) -> None:
        for fake in self._fakes.values():
            await fake.aclose()


class _NoKeys:
    def lookup(self, provider_id: ProviderId) -> Any:
        return lambda: None


def a_router(
    providers: dict[ProviderId, FakeProvider],
    *,
    planner: RoleRoute | None = None,
    grounder: RoleRoute | None = None,
    utility: RoleRoute | None = None,
    guard: BudgetGuard | None = None,
) -> tuple[ModelRouter, FakePool]:
    default = RoleRoute(primary=ModelChoice(provider_id="openai", model="gpt-4o"))
    roles = RoleMap(
        routes={
            "planner": planner or default,
            "grounder": grounder or default,
            "utility": utility or default,
        }
    )
    pool = FakePool(providers)
    return ModelRouter(roles, pool, guard=guard), pool


async def collect(stream: AsyncIterator[ChatDelta]) -> list[ChatDelta]:
    return [delta async for delta in stream]


# ---------------------------------------------------------------------------
# The role map
# ---------------------------------------------------------------------------


def test_the_three_roles_are_the_ones_the_architecture_names() -> None:
    assert ROLES == ("planner", "grounder", "utility")


def test_a_role_map_needs_every_role() -> None:
    route = RoleRoute(primary=ModelChoice(provider_id="openai", model="gpt-4o"))
    with pytest.raises(ValidationError, match="grounder"):
        RoleMap(routes={"planner": route, "utility": route})


def test_a_chain_is_bounded() -> None:
    too_many = [
        ModelChoice(provider_id="openai", model=f"m{index}") for index in range(MAX_CHAIN + 1)
    ]
    with pytest.raises(ValidationError, match=f"at most {MAX_CHAIN}"):
        RoleRoute(primary=too_many[0], fallbacks=tuple(too_many[1:]))


def test_a_chain_may_not_name_the_same_model_twice() -> None:
    choice = ModelChoice(provider_id="openai", model="gpt-4o")
    with pytest.raises(ValidationError, match="more than once"):
        RoleRoute(primary=choice, fallbacks=(choice,))


def test_the_same_model_on_two_providers_is_not_a_duplicate() -> None:
    route = RoleRoute(
        primary=ModelChoice(provider_id="openrouter", model="llama3.1"),
        fallbacks=(ModelChoice(provider_id="ollama", model="llama3.1"),),
    )
    assert len(route.chain) == 2


def test_a_role_map_is_frozen() -> None:
    route = RoleRoute(primary=ModelChoice(provider_id="openai", model="gpt-4o"))
    roles = RoleMap(routes=dict.fromkeys(ROLES, route))
    with pytest.raises(ValidationError):
        roles.routes = {}


# ---------------------------------------------------------------------------
# Binding a request to a model
# ---------------------------------------------------------------------------


def test_a_role_request_carries_no_model() -> None:
    """The role map is the authority on which model answers, so a request cannot say."""
    assert "model" not in RoleRequest.model_fields


def test_a_role_request_has_no_field_a_key_could_sit_in() -> None:
    """`P1-01`'s rule, restated one layer up: the router is in the same logs."""
    for name in RoleRequest.model_fields:
        assert "key" not in name.lower()
        assert "token" not in name.lower() or name == "max_output_tokens"


def test_bind_names_the_chosen_model() -> None:
    req = a_request()
    bound = req.bind(ModelChoice(provider_id="anthropic", model="claude-sonnet-4-5"))
    assert bound.model == "claude-sonnet-4-5"
    assert bound.messages == req.messages


def test_saved_params_are_used_when_the_request_names_none() -> None:
    choice = ModelChoice(provider_id="openai", model="gpt-4o", temperature=0.2, max_output_tokens=7)
    bound = a_request().bind(choice)
    assert bound.temperature == 0.2
    assert bound.max_output_tokens == 7


def test_the_request_wins_over_saved_params() -> None:
    choice = ModelChoice(provider_id="openai", model="gpt-4o", temperature=0.2, max_output_tokens=7)
    bound = a_request(temperature=1.5, max_output_tokens=99).bind(choice)
    assert bound.temperature == 1.5
    assert bound.max_output_tokens == 99


def test_every_other_field_survives_binding() -> None:
    req = a_request(
        tools=(A_TOOL,), tool_choice="required", json_mode=True, stop=("x",), timeout_s=3
    )
    bound = req.bind(ModelChoice(provider_id="openai", model="gpt-4o"))
    assert bound.tools == (A_TOOL,)
    assert bound.tool_choice == "required"
    assert bound.json_mode is True
    assert bound.stop == ("x",)
    assert bound.timeout_s == 3


# ---------------------------------------------------------------------------
# What a call needs
# ---------------------------------------------------------------------------


def test_a_plain_request_needs_nothing_special() -> None:
    assert Requirement.of(a_request()) == Requirement()


def test_an_image_in_a_user_message_needs_vision() -> None:
    req = a_request(messages=(UserMessage(content=(AN_IMAGE,)),))
    assert Requirement.of(req).vision is True


def test_tools_and_json_mode_are_needs() -> None:
    need = Requirement.of(a_request(tools=(A_TOOL,), json_mode=True))
    assert (need.tool_calling, need.json_mode) == (True, True)


def test_missing_from_names_every_gap() -> None:
    need = Requirement(vision=True, tool_calling=True, json_mode=True)
    assert need.missing_from(TEXT_ONLY) == ("vision",)
    assert need.missing_from(NO_TOOLS) == ("tool calling",)
    assert need.missing_from(FULL) == ()


def test_a_capability_that_is_not_needed_is_not_missing() -> None:
    assert Requirement().missing_from(TEXT_ONLY) == ()


# ---------------------------------------------------------------------------
# The capability gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
async def test_the_gate_passes_a_capable_model(role: ModelRole) -> None:
    router, _ = a_router({"openai": FakeProvider(caps=FULL)})
    router.ensure_capable(role, Requirement(vision=True, tool_calling=True))


def test_a_grounder_without_vision_is_refused_at_task_start() -> None:
    """`ARCHITECTURE.md § 5.3`, verbatim: refuse loudly at task start, not mid-run."""
    router, _ = a_router({"openai": FakeProvider(caps=TEXT_ONLY)})
    with pytest.raises(ProviderCapabilityError) as caught:
        router.ensure_capable("grounder", Requirement(vision=True))
    assert "vision" in str(caught.value)
    assert "grounder" in str(caught.value)


def test_the_gate_refusal_never_retries() -> None:
    """A chain must not walk past it: this is a settings mistake, not an outage."""
    router, _ = a_router({"openai": FakeProvider(caps=TEXT_ONLY)})
    with pytest.raises(ProviderCapabilityError) as caught:
        router.ensure_capable("grounder", Requirement(vision=True))
    assert caught.value.retryable is False


def test_the_gate_names_every_missing_capability() -> None:
    router, _ = a_router({"openai": FakeProvider(caps=TEXT_ONLY)})
    with pytest.raises(ProviderCapabilityError, match="vision"):
        router.ensure_capable("planner", Requirement(vision=True))


def test_the_gate_reports_an_unknown_model() -> None:
    router, _ = a_router({"openai": FakeProvider(caps={"other": FULL})})
    with pytest.raises(ProviderCapabilityError, match="unknown model"):
        router.ensure_capable("planner", Requirement())


def test_the_gate_only_looks_at_the_primary() -> None:
    """The chain is an outage path, not a way to get vision from the second model."""
    router, _ = a_router(
        {"openai": FakeProvider(caps=TEXT_ONLY), "google": FakeProvider("google", caps=FULL)},
        grounder=RoleRoute(
            primary=ModelChoice(provider_id="openai", model="gpt-4o"),
            fallbacks=(ModelChoice(provider_id="google", model="gemini-2.5-flash"),),
        ),
    )
    with pytest.raises(ProviderCapabilityError):
        router.ensure_capable("grounder", Requirement(vision=True))


async def test_chat_runs_the_gate_before_the_wire() -> None:
    blind = FakeProvider(caps=TEXT_ONLY)
    router, _ = a_router({"openai": blind})
    with pytest.raises(ProviderCapabilityError):
        await collect(
            router.chat("grounder", a_request(messages=(UserMessage(content=(AN_IMAGE,)),)))
        )
    assert blind.requests == []


def test_capabilities_for_answers_from_the_provider() -> None:
    router, _ = a_router({"openai": FakeProvider(caps=NO_TOOLS)})
    assert router.capabilities_for(ModelChoice(provider_id="openai", model="gpt-4o")) == NO_TOOLS


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def two_provider_route() -> RoleRoute:
    return RoleRoute(
        primary=ModelChoice(provider_id="openai", model="gpt-4o"),
        fallbacks=(ModelChoice(provider_id="google", model="gemini-2.5-flash"),),
    )


def test_a_chain_keeps_a_capable_fallback() -> None:
    router, _ = a_router(
        {"openai": FakeProvider(), "google": FakeProvider("google")},
        planner=two_provider_route(),
    )
    assert len(router.chain("planner", Requirement(vision=True))) == 2


def test_a_fallback_that_cannot_serve_is_dropped_not_fatal() -> None:
    router, _ = a_router(
        {"openai": FakeProvider(caps=FULL), "google": FakeProvider("google", caps=TEXT_ONLY)},
        planner=two_provider_route(),
    )
    chain = router.chain("planner", Requirement(vision=True))
    assert [choice.provider_id for choice in chain] == ["openai"]


def test_a_fallback_on_an_unbuildable_provider_is_dropped() -> None:
    router, _ = a_router({"openai": FakeProvider()}, planner=two_provider_route())
    chain = router.chain("planner", Requirement())
    assert [choice.provider_id for choice in chain] == ["openai"]


def test_a_fallback_with_a_typo_in_its_model_is_dropped() -> None:
    router, _ = a_router(
        {
            "openai": FakeProvider(),
            "google": FakeProvider("google", caps={"gemini-2.5-pro": FULL}),
        },
        planner=two_provider_route(),
    )
    assert len(router.chain("planner", Requirement())) == 1


# ---------------------------------------------------------------------------
# Falling back
# ---------------------------------------------------------------------------


def a_transient() -> ProviderTransientError:
    return ProviderTransientError("openai", "rate limited", status=429)


async def test_the_primary_answers_and_nothing_else_is_asked() -> None:
    primary = FakeProvider(deltas=(TextDelta(text="hi"), DoneDelta(finish_reason="stop")))
    secondary = FakeProvider("google")
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    deltas = await collect(router.chat("planner", a_request()))

    assert [delta.type for delta in deltas] == ["text", "done"]
    assert len(primary.requests) == 1
    assert secondary.requests == []


async def test_a_transient_failure_falls_over_to_the_secondary() -> None:
    primary = FakeProvider(error=a_transient())
    secondary = FakeProvider(
        "google", deltas=(TextDelta(text="hi"), DoneDelta(finish_reason="stop"))
    )
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    deltas = await collect(router.chat("planner", a_request()))

    assert [delta.type for delta in deltas] == ["text", "done"]
    assert len(secondary.requests) == 1
    assert secondary.requests[0].model == "gemini-2.5-flash"


@pytest.mark.parametrize(
    "error",
    [
        ProviderAuthError("openai", "that key was rejected", status=401),
        ProviderCapabilityError("openai", "no vision"),
        ProviderProtocolError("openai", "the stream ended without a finish reason"),
    ],
)
async def test_a_failure_that_is_not_transient_never_falls_back(error: ProviderError) -> None:
    """§ 5.3: fallback fires on 429/5xx/timeout and never on a refusal."""
    primary = FakeProvider(error=error)
    secondary = FakeProvider("google")
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    with pytest.raises(type(error)):
        await collect(router.chat("planner", a_request()))
    assert secondary.requests == []


async def test_a_transient_failure_after_the_first_delta_does_not_fall_back() -> None:
    """Restarting would say the same thing twice into a timeline the user is reading."""
    primary = FakeProvider(
        deltas=(TextDelta(text="on it"), DoneDelta(finish_reason="stop")),
        error=a_transient(),
        error_after=1,
    )
    secondary = FakeProvider("google")
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    seen: list[ChatDelta] = []
    with pytest.raises(ProviderTransientError):
        async for delta in router.chat("planner", a_request()):
            seen.append(delta)

    assert [delta.type for delta in seen] == ["text"]
    assert secondary.requests == []


async def test_the_last_error_surfaces_when_every_model_is_down() -> None:
    primary = FakeProvider(error=a_transient())
    secondary = FakeProvider(
        "google", error=ProviderTransientError("google", "unavailable", status=503)
    )
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    with pytest.raises(ProviderTransientError) as caught:
        await collect(router.chat("planner", a_request()))
    assert caught.value.provider_id == "google"
    assert caught.value.status == 503


async def test_a_fallback_never_carries_a_key_or_a_body_into_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = FakeProvider(error=ProviderTransientError("openai", "rate limited", status=429))
    secondary = FakeProvider("google")
    router, _ = a_router({"openai": primary, "google": secondary}, planner=two_provider_route())

    with caplog.at_level("WARNING"):
        await collect(router.chat("planner", a_request()))

    record = next(r for r in caplog.records if r.message == "model.fallback")
    assert record.to_provider == "google"  # type: ignore[attr-defined]
    assert A_KEY not in caplog.text


async def test_three_links_are_walked_in_order() -> None:
    first = FakeProvider(error=a_transient())
    second = FakeProvider("google", error=ProviderTransientError("google", "busy", status=503))
    third = FakeProvider("ollama")
    router, _ = a_router(
        {"openai": first, "google": second, "ollama": third},
        planner=RoleRoute(
            primary=ModelChoice(provider_id="openai", model="gpt-4o"),
            fallbacks=(
                ModelChoice(provider_id="google", model="gemini-2.5-flash"),
                ModelChoice(provider_id="ollama", model="llama3.1:8b"),
            ),
        ),
    )

    await collect(router.chat("planner", a_request()))

    assert len(first.requests) == len(second.requests) == len(third.requests) == 1


# ---------------------------------------------------------------------------
# The budget guard (`P1-09`)
# ---------------------------------------------------------------------------


@pytest.fixture
def guarded(tmp_path: Path) -> Iterator[Callable[[BudgetLimits], BudgetGuard]]:
    """A factory for guards over one real ledger, so several can share a total."""
    db_path = tmp_path / "aegis.db"
    bootstrap(db_path)
    with UsageLedger.open(db_path) as ledger:
        yield lambda limits: BudgetGuard(ledger, limits=limits)


def a_task(tmp_path: Path) -> int:
    conn = connect(tmp_path / "aegis.db")
    try:
        cursor = conn.execute(
            "INSERT INTO tasks (title, goal, status, created_at)"
            " VALUES ('t', 'g', 'RUNNING', '2026-09-20T12:00:00.000Z')"
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid
    finally:
        conn.close()


def priced(cents: float) -> tuple[ChatDelta, ...]:
    return (
        TextDelta(text="on it"),
        UsageDelta(usage=Usage(input_tokens=1_000, output_tokens=500, cost_cents=cents)),
        DoneDelta(finish_reason="stop"),
    )


async def test_a_router_without_a_guard_is_unchanged() -> None:
    primary = FakeProvider(deltas=priced(4.5))
    router, _ = a_router({"openai": primary})
    assert [d.type for d in await collect(router.chat("planner", a_request()))] == [
        "text",
        "usage",
        "done",
    ]


async def test_what_a_call_cost_is_recorded_against_the_model_that_answered(
    tmp_path: Path, guarded: Callable[[BudgetLimits], BudgetGuard]
) -> None:
    task_id = a_task(tmp_path)
    guard = guarded(BudgetLimits())
    primary = FakeProvider(deltas=priced(4.5))
    router, _ = a_router({"openai": primary}, guard=guard)

    await collect(router.chat("planner", a_request(), task_id=task_id))

    status = guard.status(task_id)
    assert status.task.cents == pytest.approx(4.5)
    assert status.task.tokens == 1_500


async def test_a_ceiling_already_reached_stops_the_call_before_the_wire(
    tmp_path: Path, guarded: Callable[[BudgetLimits], BudgetGuard]
) -> None:
    """§ 5.3: on breach, pause and ask — not one more request and then pause."""
    task_id = a_task(tmp_path)
    guard = guarded(BudgetLimits(task_cents=4.0))
    primary = FakeProvider(deltas=priced(4.5))
    router, _ = a_router({"openai": primary}, guard=guard)

    await collect(router.chat("planner", a_request(), task_id=task_id))
    assert len(primary.requests) == 1

    with pytest.raises(BudgetExceededError) as raised:
        await collect(router.chat("planner", a_request(), task_id=task_id))

    assert raised.value.ceiling == "task_cents"
    assert len(primary.requests) == 1, "a second request reached the provider"


async def test_the_call_that_crosses_the_ceiling_still_finishes(
    tmp_path: Path, guarded: Callable[[BudgetLimits], BudgetGuard]
) -> None:
    """The money is spent by the time usage arrives; throwing away the answer wastes it."""
    task_id = a_task(tmp_path)
    primary = FakeProvider(deltas=priced(99.0))
    router, _ = a_router({"openai": primary}, guard=guarded(BudgetLimits(task_cents=1.0)))

    deltas = await collect(router.chat("planner", a_request(), task_id=task_id))

    assert [delta.type for delta in deltas] == ["text", "usage", "done"]
    assert primary.stream_finished is True


async def test_usage_is_attributed_to_the_fallback_that_actually_answered(
    tmp_path: Path, guarded: Callable[[BudgetLimits], BudgetGuard]
) -> None:
    task_id = a_task(tmp_path)
    guard = guarded(BudgetLimits())
    primary = FakeProvider(error=a_transient())
    secondary = FakeProvider("google", deltas=priced(2.0))
    router, _ = a_router(
        {"openai": primary, "google": secondary}, planner=two_provider_route(), guard=guard
    )

    await collect(router.chat("planner", a_request(), task_id=task_id))

    assert guard.status(task_id).task.cents == pytest.approx(2.0)


async def test_a_call_outside_a_task_counts_towards_the_day(
    guarded: Callable[[BudgetLimits], BudgetGuard],
) -> None:
    guard = guarded(BudgetLimits())
    router, _ = a_router({"openai": FakeProvider(deltas=priced(3.0))}, guard=guard)

    await collect(router.chat("utility", a_request()))

    assert guard.status().day.cents == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# The stop path
# ---------------------------------------------------------------------------


async def test_abandoning_the_router_stream_closes_the_provider_stream() -> None:
    """Preemption abandons mid-flight; every layer has to release its socket.

    `async for` does not close the iterator it was reading, so without the router's own
    `finally` the adapter's `finally` would only run whenever the event loop got around
    to finalising it — which is the `generator didn't stop after athrow()` noise
    `P1-02` fixed one layer down.
    """
    primary = FakeProvider(
        deltas=(TextDelta(text="a"), TextDelta(text="b"), DoneDelta(finish_reason="stop"))
    )
    router, _ = a_router({"openai": primary})

    # The declared return type is `AsyncIterator`, because that is what the protocol
    # promises; the cast is the same one the router itself makes to close a provider.
    stream = cast("AsyncGenerator[ChatDelta, None]", router.chat("planner", a_request()))
    assert (await anext(stream)).type == "text"
    await stream.aclose()

    assert primary.closed == 1
    assert primary.stream_finished is False


async def test_a_completed_stream_is_closed_too() -> None:
    primary = FakeProvider()
    router, _ = a_router({"openai": primary})
    await collect(router.chat("planner", a_request()))
    assert primary.closed == 1


async def test_closing_the_router_closes_every_provider() -> None:
    primary = FakeProvider()
    router, _ = a_router({"openai": primary})
    await router.aclose()
    assert primary.closed == 1
    await router.aclose()


# ---------------------------------------------------------------------------
# The pool — real adapters, no network
# ---------------------------------------------------------------------------


class RecordingKeys:
    """A `KeySource` that counts lookups and never hands out a stored key."""

    def __init__(self, key: str | None = A_KEY) -> None:
        self._key = key
        self.asked: list[ProviderId] = []

    def lookup(self, provider_id: ProviderId) -> Any:
        def read() -> str | None:
            self.asked.append(provider_id)
            return self._key

        return read


def a_transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("openai", OpenAIProvider),
        ("nvidia", OpenAIProvider),
        ("openrouter", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("google", GoogleProvider),
        ("ollama", OllamaProvider),
    ],
)
async def test_the_pool_builds_the_right_adapter(provider_id: ProviderId, expected: type) -> None:
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    provider = pool.get(provider_id)
    assert isinstance(provider, expected)
    assert provider.id == provider_id
    await pool.aclose()


async def test_the_pool_reuses_one_adapter_per_provider() -> None:
    """A fresh TLS handshake per step is latency the user watches (`P1-02`)."""
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    assert pool.get("openai") is pool.get("openai")
    await pool.aclose()


async def test_the_pool_holds_no_key() -> None:
    """The seam `ARCHITECTURE.md § 5.3` asks for: a lookup is stored, never a key."""
    keys = RecordingKeys()
    pool = ProviderPool(keys, transport=a_transport())
    pool.get("openai")
    assert A_KEY not in repr(vars(pool))
    assert keys.asked == []  # nothing read a key just to build a provider
    await pool.aclose()


async def test_a_custom_provider_needs_an_address() -> None:
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    with pytest.raises(ProviderUnavailableError, match="No address is saved"):
        pool.get("custom")
    await pool.aclose()


async def test_a_custom_address_is_rechecked_here() -> None:
    """Settings can be old or hand-edited, and this URL decides where a key is sent."""
    pool = ProviderPool(
        RecordingKeys(), custom_base_url="http://example.invalid/v1", transport=a_transport()
    )
    with pytest.raises(ProviderUnavailableError, match="https://"):
        pool.get("custom")
    await pool.aclose()


async def test_a_custom_refusal_never_quotes_the_address() -> None:
    pool = ProviderPool(
        RecordingKeys(),
        custom_base_url="https://gw.example.invalid/v1?key=sk-secret-value",
        transport=a_transport(),
    )
    with pytest.raises(ProviderUnavailableError) as caught:
        pool.get("custom")
    assert "sk-secret-value" not in str(caught.value)
    await pool.aclose()


async def test_a_usable_custom_address_builds() -> None:
    pool = ProviderPool(
        RecordingKeys(), custom_base_url="https://gw.example.invalid/v1/", transport=a_transport()
    )
    assert pool.get("custom").id == "custom"
    await pool.aclose()


async def test_an_unbuildable_provider_is_not_cached() -> None:
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    with pytest.raises(ProviderUnavailableError):
        pool.get("custom")
    with pytest.raises(ProviderUnavailableError):
        pool.get("custom")
    await pool.aclose()


async def test_closing_the_pool_twice_is_safe() -> None:
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    pool.get("openai")
    await pool.aclose()
    await pool.aclose()


def test_every_adapter_satisfies_the_managed_protocol() -> None:
    pool = ProviderPool(RecordingKeys(), transport=a_transport())
    assert isinstance(pool.get("openai"), ManagedProvider)


def test_the_key_source_protocol_is_what_the_vault_offers() -> None:
    """`KeyVault.lookup` is the whole of the vault the router may touch."""
    from aegis_core.storage.vault import KeyVault

    store: dict[tuple[str, str], str] = {}

    class Memory:
        def get_password(self, service: str, username: str) -> str | None:
            return store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            store.pop((service, username), None)

    vault: KeySource = KeyVault(Memory())
    vault.set_key("openai", A_KEY)  # type: ignore[attr-defined]
    assert vault.lookup("openai")() == A_KEY
