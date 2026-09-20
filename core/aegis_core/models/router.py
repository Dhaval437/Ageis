"""The router — roles in, one provider's stream out (`ARCHITECTURE.md § 5.3`).

Everything above this line asks for a **role**, never a provider. `ARCHITECTURE.md § 5.2`
says the agent uses three of them — `PLANNER`, `GROUNDER`, `UTILITY` — and the user maps
each to any model they like, so `P4` names the job and the user's settings decide who
does it. This module owns the three things that decision needs:

- **The role map.** `{role: {provider_id, model, params}}`, primary plus a bounded
  fallback chain, exactly as § 5.2 says it is persisted.
- **The fallback chain.** It fires on `ProviderTransientError` and on nothing else
  (§ 5.3), and **only before the first delta has reached the consumer** — once text is in
  the timeline, restarting on another provider would say the same thing twice.
- **The capability gate.** A role whose model cannot do what the task needs is refused
  *at task start*, loudly, naming the model and what is missing — not mid-run as a
  provider 400 with a task half done.

- **The budget guard.** `models/budget.py` (`P1-09`) decides *whether to ask at all*,
  and this module is where it is asked: a `chat()` on a router that has one is checked
  before anything reaches the wire, and every `UsageDelta` that comes back is recorded
  against the task. A router built without a guard behaves exactly as it did before.

It also owns the provider instances, because they own connection pools: `ProviderPool`
builds one adapter per provider id and `aclose()` releases them all. What it never owns
is a key. The pool is given a `KeySource` — the DPAPI vault (`P1-07`) — and hands each
adapter a `KeyLookup` callable, so a key is read from the OS per request and nothing
here, or in an adapter, outlives a call holding one (`ARCHITECTURE.md § 5.3`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Final, Literal, Protocol, runtime_checkable

import httpx2
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis_core.models.budget import BudgetGuard
from aegis_core.models.provider import (
    ModelProvider,
    ProviderCapabilityError,
    ProviderError,
)
from aegis_core.models.providers.anthropic import AnthropicProvider
from aegis_core.models.providers.common import KeyLookup
from aegis_core.models.providers.compatible import NVIDIA, OPENROUTER, custom_config
from aegis_core.models.providers.google import GoogleProvider
from aegis_core.models.providers.ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_BASE_URL
from aegis_core.models.providers.ollama import OllamaProvider
from aegis_core.models.providers.openai import OPENAI, OpenAIProvider, ProviderConfig
from aegis_core.models.schemas import (
    Capabilities,
    ChatDelta,
    ChatMessage,
    ChatRequest,
    ImagePart,
    ProviderId,
    ToolChoice,
    ToolDef,
    ToolResultMessage,
    UsageDelta,
    UserMessage,
)

log = logging.getLogger(__name__)

ModelRole = Literal["planner", "grounder", "utility"]
"""The three jobs a model does (`ARCHITECTURE.md § 5.2`). The user maps each one."""

ROLES: Final[tuple[ModelRole, ...]] = ("planner", "grounder", "utility")

#: Primary plus at most three fallbacks. `REVIEW.md § 2` forbids an unbounded anything,
#: and a chain longer than this is a user who has misunderstood what it is for: it is an
#: outage path, not a way to try every model they own.
MAX_CHAIN: Final = 4


class RouterError(Exception):
    """The router could not route. Shown to the user, so it never quotes a key."""


class ProviderUnavailableError(RouterError):
    """A provider this build supports cannot be built from the current settings.

    A `custom` gateway with no address saved, or one whose address is not usable. It is
    a settings problem the user can fix, not a provider that failed — so it is not a
    `ProviderError` and the fallback chain does not treat it as a hiccup.
    """


# ---------------------------------------------------------------------------
# The role map
# ---------------------------------------------------------------------------


class ModelChoice(BaseModel):
    """One model, chosen for one slot in one role's chain.

    `temperature` and `max_output_tokens` are the *params* of § 5.2's
    `{role: {provider_id, model_id, params}}`: they are what the user picked for this
    model, and a request that names its own wins over them, because the caller knows
    what this particular call needs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: ProviderId
    model: str = Field(min_length=1, description="The provider's own model id.")
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)


class RoleRoute(BaseModel):
    """Who answers for one role, and who answers when they cannot.

    `ARCHITECTURE.md § 5.3` spells the chain `primary → secondary → local`. It is a list
    rather than two named slots because "local" is only a convention — nothing here
    requires the last link to be Ollama — but it is bounded, and a chain that names the
    same model twice is a typo, not a retry policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: ModelChoice
    fallbacks: tuple[ModelChoice, ...] = ()

    @model_validator(mode="after")
    def _bounded_and_distinct(self) -> RoleRoute:
        chain = self.chain
        if len(chain) > MAX_CHAIN:
            raise ValueError(f"a role may name at most {MAX_CHAIN} models")
        seen = {(choice.provider_id, choice.model) for choice in chain}
        if len(seen) != len(chain):
            raise ValueError("a role names the same model more than once")
        return self

    @property
    def chain(self) -> tuple[ModelChoice, ...]:
        """The primary first, then each fallback in order."""
        return (self.primary, *self.fallbacks)


class RoleMap(BaseModel):
    """Every role mapped. All three are required: an unmapped role is a task that dies.

    Persisted as § 5.2 describes. Where the *defaults* come from is `P1-10`'s question —
    this type is what the Models screen writes and the router reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    routes: Mapping[ModelRole, RoleRoute]

    @model_validator(mode="after")
    def _every_role_mapped(self) -> RoleMap:
        missing = [role for role in ROLES if role not in self.routes]
        if missing:
            raise ValueError(f"no model is mapped for {', '.join(missing)}")
        return self

    def route(self, role: ModelRole) -> RoleRoute:
        return self.routes[role]


# ---------------------------------------------------------------------------
# What a call needs, and what the gate checks it against
# ---------------------------------------------------------------------------


class Requirement(BaseModel):
    """What a model must be able to do to serve a call.

    Built from a request by `Requirement.of`, or stated by `P4` at task start — which is
    the point of it. § 5.3 wants a `GROUNDER` with no vision refused *before* the task
    runs, and at that moment there is no request yet, only the knowledge that there will
    be screenshots.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vision: bool = False
    tool_calling: bool = False
    json_mode: bool = False

    @classmethod
    def of(cls, req: RoleRequest) -> Requirement:
        return cls(
            vision=any(_carries_an_image(message) for message in req.messages),
            tool_calling=bool(req.tools),
            json_mode=req.json_mode,
        )

    def missing_from(self, caps: Capabilities) -> tuple[str, ...]:
        """Which of the three this model does not have. Empty means it can serve."""
        return tuple(
            name
            for name, needed, has in (
                ("vision", self.vision, caps.vision),
                ("tool calling", self.tool_calling, caps.tool_calling),
                ("JSON mode", self.json_mode, caps.json_mode),
            )
            if needed and not has
        )


def _carries_an_image(message: ChatMessage) -> bool:
    return isinstance(message, UserMessage | ToolResultMessage) and any(
        isinstance(part, ImagePart) for part in message.content
    )


class RoleRequest(BaseModel):
    """A `ChatRequest` with no model in it, because the role map picks the model.

    Every field of `ChatRequest` except `model`. It is a separate type rather than a
    `ChatRequest` whose model is ignored, because a request that carries a model the
    router then overwrites is one a caller will eventually believe — and each attempt in
    a fallback chain genuinely runs a *different* model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    tools: tuple[ToolDef, ...] = ()
    tool_choice: ToolChoice = "auto"
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)
    json_mode: bool = False
    stop: tuple[str, ...] = ()
    timeout_s: float = Field(default=120.0, gt=0)

    def bind(self, choice: ModelChoice) -> ChatRequest:
        """The request as it goes to one particular model.

        The request's own `temperature` / `max_output_tokens` win over the user's saved
        params: the caller is asking for this call, the params describe the model.
        """
        return ChatRequest(
            model=choice.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice=self.tool_choice,
            temperature=self.temperature if self.temperature is not None else choice.temperature,
            max_output_tokens=(
                self.max_output_tokens
                if self.max_output_tokens is not None
                else choice.max_output_tokens
            ),
            json_mode=self.json_mode,
            stop=self.stop,
            timeout_s=self.timeout_s,
        )


# ---------------------------------------------------------------------------
# The providers
# ---------------------------------------------------------------------------


@runtime_checkable
class ManagedProvider(ModelProvider, Protocol):
    """A `ModelProvider` whose connection pool somebody has to release.

    Every adapter has an `aclose()`; the protocol does not require one because a
    `ModelProvider` need not hold a socket. The pool builds the ones that do.
    """

    async def aclose(self) -> None: ...


@runtime_checkable
class _Closable(Protocol):
    async def aclose(self) -> None: ...


class KeySource(Protocol):
    """The one thing the pool needs from the vault: a lookup, never a key.

    Declared structurally so `storage.vault.KeyVault` satisfies it without this module
    importing storage, and so a test can supply keys without touching DPAPI.
    """

    def lookup(self, provider_id: ProviderId) -> KeyLookup: ...


class ProviderPool:
    """One adapter per provider id, built when it is first asked for.

    Lazy because building a `custom` provider validates a URL the user typed and building
    any of them opens a connection pool — a user who has mapped every role to one
    provider should pay for one. The instances are shared across roles and across calls
    for the reason `P1-02` gives: a fresh TLS handshake on every step of the agent loop
    is latency the user watches.
    """

    def __init__(
        self,
        keys: KeySource,
        *,
        custom_base_url: str | None = None,
        ollama_base_url: str = OLLAMA_DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._keys = keys
        self._custom_base_url = custom_base_url
        self._ollama_base_url = ollama_base_url
        self._transport = transport
        self._providers: dict[ProviderId, ManagedProvider] = {}

    def get(self, provider_id: ProviderId) -> ManagedProvider:
        """The adapter for `provider_id`, building it the first time.

        Raises `ProviderUnavailableError` when settings cannot describe one — today only
        a `custom` gateway with a missing or unusable address.
        """
        provider = self._providers.get(provider_id)
        if provider is None:
            provider = self._build(provider_id)
            self._providers[provider_id] = provider
        return provider

    async def aclose(self) -> None:
        """Release every pool built so far. Safe to call more than once."""
        providers, self._providers = self._providers, {}
        for provider in providers.values():
            await provider.aclose()

    def _build(self, provider_id: ProviderId) -> ManagedProvider:
        lookup = self._keys.lookup(provider_id)
        if provider_id == "anthropic":
            return AnthropicProvider(lookup, transport=self._transport)
        if provider_id == "google":
            return GoogleProvider(lookup, transport=self._transport)
        if provider_id == "ollama":
            # A local server needs no key, but a saved one is still sent: a proxy in
            # front of it may want one (`P1-06`).
            return OllamaProvider(lookup, base_url=self._ollama_base_url, transport=self._transport)
        if provider_id == "custom":
            return OpenAIProvider(lookup, config=self._custom_config(), transport=self._transport)
        config = {"openai": OPENAI, "nvidia": NVIDIA, "openrouter": OPENROUTER}[provider_id]
        return OpenAIProvider(lookup, config=config, transport=self._transport)

    def _custom_config(self) -> ProviderConfig:
        """The user's gateway, re-checked here rather than trusted from settings.

        `normalise_base_url` (`P1-05`) decides where a key may be sent, and settings can
        have been written by an older build or edited by hand. Its `ValueError` carries a
        message written for the user and never quotes the address back, so it is safe to
        pass straight through.
        """
        if not self._custom_base_url:
            raise ProviderUnavailableError(
                "No address is saved for the custom gateway. Add one in Models."
            )
        try:
            return custom_config(self._custom_base_url)
        except ValueError as exc:
            raise ProviderUnavailableError(str(exc)) from exc


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class ModelRouter:
    """Turns a role into an answer.

    Holds the role map and the pool. Nothing above it names a provider, and nothing in it
    holds a key.

    `guard` is optional because a router is useful without one — `P1-10`'s *Test* button
    spends money outside any task, and the tests for routing itself have no ledger. With
    one, every call is checked before it is made and accounted for after.
    """

    def __init__(
        self,
        roles: RoleMap,
        pool: ProviderPool,
        *,
        guard: BudgetGuard | None = None,
    ) -> None:
        self._roles = roles
        self._pool = pool
        self._guard = guard

    @property
    def roles(self) -> RoleMap:
        return self._roles

    async def aclose(self) -> None:
        """Release every provider the pool built. Safe to call more than once."""
        await self._pool.aclose()

    # -- the capability gate -----------------------------------------------

    def capabilities_for(self, choice: ModelChoice) -> Capabilities:
        """What one chosen model can do. `P1-09` prices a call from this."""
        return self._pool.get(choice.provider_id).capabilities(choice.model)

    def ensure_capable(self, role: ModelRole, need: Requirement) -> None:
        """Refuse now, loudly, if this role's model cannot do what the task needs.

        `ARCHITECTURE.md § 5.3`: *if a task step needs vision and the chosen `GROUNDER`
        has none, refuse loudly at task start, not mid-run.* Only the **primary** is
        gated — the chain exists for an outage, not as a way to quietly get vision from
        the second model when the user asked for the first.

        Raises `ProviderCapabilityError` (never retryable, so no chain walks past it), or
        `ProviderUnavailableError` if settings cannot even build the provider.
        """
        primary = self._roles.route(role).primary
        caps = self.capabilities_for(primary)
        missing = need.missing_from(caps)
        if missing:
            raise ProviderCapabilityError(
                primary.provider_id,
                f"{primary.model} is mapped to {role} but has no {' or '.join(missing)}",
            )

    def chain(self, role: ModelRole, need: Requirement) -> tuple[ModelChoice, ...]:
        """The models to try, in order, for a call that needs `need`.

        The primary is gated and kept. A fallback that cannot serve this call is dropped
        with a log line rather than failing the call: falling over to a model that cannot
        see would answer a vision question with nothing, and the user would be told their
        primary is down, not that their secondary is blind.
        """
        self.ensure_capable(role, need)
        route = self._roles.route(role)
        usable = [route.primary]
        for choice in route.fallbacks:
            reason = self._unusable(choice, need)
            if reason is None:
                usable.append(choice)
            else:
                log.info(
                    "model.fallback_skipped",
                    extra={
                        "role": role,
                        "provider_id": choice.provider_id,
                        "model": choice.model,
                        "reason": reason,
                    },
                )
        return tuple(usable)

    def _unusable(self, choice: ModelChoice, need: Requirement) -> str | None:
        """Why this fallback cannot serve the call, or `None` if it can."""
        try:
            caps = self.capabilities_for(choice)
        except ProviderUnavailableError as exc:
            return str(exc)
        except ProviderCapabilityError as exc:
            # An unknown model id on a provider that can enumerate its own: a typo in a
            # fallback must not take out a role whose primary is fine.
            return exc.reason
        missing = need.missing_from(caps)
        return f"no {' or '.join(missing)}" if missing else None

    # -- the call ----------------------------------------------------------

    async def chat(
        self,
        role: ModelRole,
        req: RoleRequest,
        *,
        task_id: int | None = None,
    ) -> AsyncIterator[ChatDelta]:
        """Stream one completion from whichever model serves `role`.

        Walks the chain on `ProviderTransientError` — a 429, a 5xx, a timeout — and on
        nothing else (`ARCHITECTURE.md § 5.3`): a rejected key, a refusal, or a malformed
        stream is not a hiccup, and retrying it elsewhere is shopping for a yes.

        **A chain is only walked before the first delta reaches the consumer.** Once text
        has been streamed into the timeline, another provider starting from the top would
        repeat it, and a tool call already emitted would be asked for twice. After that
        point a transient failure is the caller's problem, which is what `P4-08`'s
        cancellation and retry are for.

        With a budget guard, this raises `BudgetExceededError` **before any request is
        made** if a ceiling has already been reached, and records what each attempt cost
        as its `UsageDelta` arrives. A breach discovered while recording does not abort
        the stream in flight — that money is spent, and the refusal belongs at the start
        of the next call (`models/budget.py`).
        """
        if self._guard is not None:
            self._guard.check(task_id)

        chain = self.chain(role, Requirement.of(req))
        last: ProviderError | None = None

        for attempt, choice in enumerate(chain):
            provider = self._pool.get(choice.provider_id)
            stream = provider.chat(req.bind(choice))
            delivered = False
            try:
                async for delta in stream:
                    delivered = True
                    if isinstance(delta, UsageDelta) and self._guard is not None:
                        self._guard.record(
                            delta.usage,
                            role=role,
                            provider_id=choice.provider_id,
                            model=choice.model,
                            task_id=task_id,
                        )
                    yield delta
                return
            except ProviderError as error:
                if delivered or not error.retryable or attempt == len(chain) - 1:
                    raise
                last = error
                log.warning(
                    "model.fallback",
                    extra={
                        "role": role,
                        "from_provider": choice.provider_id,
                        "from_model": choice.model,
                        "to_provider": chain[attempt + 1].provider_id,
                        "to_model": chain[attempt + 1].model,
                        "reason": error.reason,
                        "status": error.status,
                    },
                )
            finally:
                # Close the adapter's generator explicitly. `async for` does not, and the
                # consumer abandoning *this* generator — preemption, a budget breach —
                # throws `GeneratorExit` at the `yield` above and leaves the adapter's
                # stream suspended on a socket that is about to go (`REMEMBER.md § 5`,
                # 2026-09-17). One link of the stop path per layer, all of them closed.
                if isinstance(stream, _Closable):
                    await stream.aclose()

        if last is not None:  # pragma: no cover — the last attempt raises, it never falls out
            raise last
