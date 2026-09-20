"""What the Models screen talks to (`UI.md § 8.4`, `P1-10`).

`P1-01`..`P1-09` built a model layer with no way in. This is the way in: one object that
owns the three durable things a user's model configuration is made of — the settings
document (`storage/settings.py`), the keys (`storage/vault.py`) and the spend ledger
(`storage/usage.py`) — and answers the five questions the screen asks.

- **What can I choose?** `catalog()`. Built from what *this build* knows plus what a
  server on *this machine* answers, and nothing else: a provider whose catalogue cannot
  be enumerated offline reports `free_text_model` and the user types the id. The screen
  has to work on a machine with no internet, because it is the screen you open when
  nothing works.
- **What did I choose?** `settings()` / `save_settings()`. The wire shapes are
  `server/schemas.py`'s and the model layer's are `models/router.py`'s; this module is
  the only place that converts, so a role that is not mapped yet exists in exactly one
  of the two type systems.
- **Does this key work?** `validate()`. One real call to the provider, timed.
- **What have I spent?** `spend()`. The first value for the meter; `cost.updated` keeps
  it live (invariant 15).
- **Who answers for this role?** `router()`, for `P4`. `None` until all three roles are
  mapped — a task with an unmapped role is one that dies at its first step.

**It never returns a key.** `has_key` and `mask_key()`'s `sk-…abcd` are the only two
things it will say about one (`ARCHITECTURE.md § 5.3`), and a key only ever travels the
other way, into `set_key()`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Final

import httpx2

from aegis_core.models.budget import BudgetGuard, BudgetLimits, EventPublisher
from aegis_core.models.provider import ProviderError
from aegis_core.models.providers.anthropic import ANTHROPIC_MODELS
from aegis_core.models.providers.compatible import normalise_base_url
from aegis_core.models.providers.google import GOOGLE_MODELS
from aegis_core.models.providers.ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_BASE_URL
from aegis_core.models.providers.ollama import detect
from aegis_core.models.providers.openai import OPENAI_MODELS
from aegis_core.models.router import (
    ROLES,
    ModelChoice,
    ModelRole,
    ModelRouter,
    ProviderPool,
    RoleMap,
    RoleRoute,
    RouterError,
)
from aegis_core.models.schemas import Capabilities, ProviderId
from aegis_core.server.schemas import (
    BudgetLimitsSpec,
    ModelCatalog,
    ModelChoiceSpec,
    ModelInfo,
    ModelSettings,
    ProviderCatalog,
    RoleRouteSpec,
    SpendResponse,
    SpendTotals,
    ValidateResponse,
)
from aegis_core.storage.settings import SettingsStore
from aegis_core.storage.usage import UsageLedger
from aegis_core.storage.vault import KeyVault, VaultError

log = logging.getLogger(__name__)

#: The `settings` row this module owns. One document, replaced whole.
SETTINGS_KEY: Final = "models"


class ProviderCard:
    """The fixed half of a provider card: what it is called and how it behaves.

    Everything here is a property of the *adapter*, decided at build time. What the user
    has done about it — a key, an address, which models a local server actually has —
    is added by `catalog()`.
    """

    def __init__(
        self,
        provider_id: ProviderId,
        label: str,
        *,
        models: Mapping[str, Capabilities] | None = None,
        local: bool = False,
        requires_key: bool = True,
        editable_base_url: bool = False,
        free_text_model: bool = False,
        detail: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.label = label
        self.models = {} if models is None else models
        self.local = local
        self.requires_key = requires_key
        self.editable_base_url = editable_base_url
        self.free_text_model = free_text_model
        self.detail = detail


#: Why three providers let the user type a model id instead of picking one: `nvidia`,
#: `openrouter` and `custom` serve catalogues this build cannot enumerate without a key
#: and a network call, and `COMPATIBLE_UNKNOWN` (`P1-05`) is exactly the answer for a
#: model we have no table for. `anthropic` and `google` have tables *and* an unknown
#: fallback, because both ship models faster than a pinned table is updated (`P1-03`,
#: `P1-04`) — so their lists are suggestions, not a fence.
_TYPE_IT_IN: Final = (
    "Aegis can't list this provider's models. Type the model id exactly as the provider spells it."
)

CARDS: Final[tuple[ProviderCard, ...]] = (
    ProviderCard("openai", "OpenAI", models=OPENAI_MODELS),
    ProviderCard("anthropic", "Anthropic", models=ANTHROPIC_MODELS, free_text_model=True),
    ProviderCard("google", "Google Gemini", models=GOOGLE_MODELS, free_text_model=True),
    ProviderCard("nvidia", "NVIDIA NIM", free_text_model=True, detail=_TYPE_IT_IN),
    ProviderCard("openrouter", "OpenRouter", free_text_model=True, detail=_TYPE_IT_IN),
    ProviderCard(
        "ollama",
        "Local (Ollama)",
        local=True,
        requires_key=False,
        editable_base_url=True,
    ),
    ProviderCard(
        "custom",
        "Custom gateway",
        editable_base_url=True,
        free_text_model=True,
        detail=_TYPE_IT_IN,
    ),
)

_OLLAMA_ABSENT: Final = (
    "Ollama isn't running on this machine. Start it, or install it from ollama.com."
)
_OLLAMA_EMPTY: Final = (
    "Ollama is running, but no models are installed. Install one with: ollama pull llama3.1"
)
_CUSTOM_NO_ADDRESS: Final = "Add the address of your gateway, then Test it."


def _info(model: str, caps: Capabilities) -> ModelInfo:
    return ModelInfo(
        id=model,
        vision=caps.vision,
        tool_calling=caps.tool_calling,
        json_mode=caps.json_mode,
        ctx_window=caps.ctx_window,
        cost_per_mtok_input=caps.cost_per_mtok_input,
        cost_per_mtok_output=caps.cost_per_mtok_output,
    )


def _sorted_models(models: Mapping[str, Capabilities]) -> list[ModelInfo]:
    return [_info(name, caps) for name, caps in sorted(models.items())]


# ---------------------------------------------------------------------------
# Wire <-> model layer
# ---------------------------------------------------------------------------


def _choice(spec: ModelChoiceSpec) -> ModelChoice:
    return ModelChoice(
        provider_id=spec.provider_id,
        model=spec.model,
        temperature=spec.temperature,
        max_output_tokens=spec.max_output_tokens,
    )


def _route(spec: RoleRouteSpec) -> RoleRoute:
    return RoleRoute(
        primary=_choice(spec.primary),
        fallbacks=tuple(_choice(link) for link in spec.fallbacks),
    )


def role_map(settings: ModelSettings) -> RoleMap | None:
    """The router's role map, or `None` while any role is still unmapped.

    Raises `ValueError` — through `RoleRoute`'s own validators — for a chain that is too
    long or names the same model twice, so a settings document that cannot be routed is
    refused at the `PUT` rather than at the first task.
    """
    specs = {role: getattr(settings, role) for role in ROLES}
    if any(spec is None for spec in specs.values()):
        return None
    routes: dict[ModelRole, RoleRoute] = {
        role: _route(spec) for role, spec in specs.items() if spec is not None
    }
    return RoleMap(routes=routes)


def budget_limits(spec: BudgetLimitsSpec) -> BudgetLimits:
    return BudgetLimits(
        task_cents=spec.task_cents,
        day_cents=spec.day_cents,
        task_tokens=spec.task_tokens,
        day_tokens=spec.day_tokens,
    )


def _totals_of(cents: float, tokens: int, unpriced: int) -> SpendTotals:
    return SpendTotals(cents=cents, tokens=tokens, unpriced_calls=unpriced)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ModelService:
    """One owner for the settings document, the vault and the ledger."""

    def __init__(
        self,
        settings_store: SettingsStore,
        vault: KeyVault,
        ledger: UsageLedger,
        *,
        publisher: EventPublisher | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._vault = vault
        self._ledger = ledger
        self._publisher = publisher
        self._transport = transport
        self._pool: ProviderPool | None = None

    # -- settings ----------------------------------------------------------

    def settings(self) -> ModelSettings:
        """What the user has configured, or the defaults if they have not.

        A document this build cannot parse is answered as *nothing configured*, with a
        logged reason: the screen that could fix it must still open.
        """
        saved = self._settings_store.get(SETTINGS_KEY)
        if saved is None:
            return ModelSettings()
        try:
            return ModelSettings.model_validate(saved)
        except ValueError as error:
            log.warning("models.settings_unreadable", extra={"error": type(error).__name__})
            return ModelSettings()

    async def save_settings(self, settings: ModelSettings) -> ModelSettings:
        """Replace the document. Raises `ValueError` for a configuration that cannot run.

        Two checks happen here rather than at the first task: a role chain that is too
        long or repeats a model, and a `custom` address that is not one a key may be sent
        to (`normalise_base_url`, `P1-05`). Both messages are written for the user, and
        neither quotes the address back.
        """
        for role in ROLES:
            spec = getattr(settings, role)
            if spec is not None:
                # `RoleRoute`'s validators, on each role that *is* mapped. `role_map`
                # would skip them all while any role is still unset, which is exactly
                # the state a half-configured screen saves in.
                _route(spec)
        checked = settings.model_copy(
            update={
                "custom_base_url": _checked_url(settings.custom_base_url),
                "ollama_base_url": _checked_url(settings.ollama_base_url),
            }
        )
        self._settings_store.put(SETTINGS_KEY, checked.model_dump(mode="json"))
        # The pool caches an adapter per provider, and two of those adapters are built
        # from addresses that just changed. Drop it rather than reason about which.
        await self._drop_pool()
        return checked

    # -- the catalogue -----------------------------------------------------

    async def catalog(self) -> ModelCatalog:
        """Every provider, what it serves, and whether it is set up.

        The only network this makes is to Ollama on loopback. Nothing here reaches a
        vendor: a catalogue fetched over the internet would make the Models screen
        unusable on the machine that needs it most.
        """
        settings = self.settings()
        local = await self._ollama_card(settings.ollama_base_url)
        return ModelCatalog(
            providers=[self._card(card, settings, local) for card in CARDS],
        )

    def _card(
        self,
        card: ProviderCard,
        settings: ModelSettings,
        local: _LocalModels,
    ) -> ProviderCatalog:
        masked, key_detail = self._key_state(card.provider_id)
        models = _sorted_models(local.models if card.provider_id == "ollama" else card.models)
        detail = card.detail
        if card.provider_id == "ollama":
            detail = local.detail
        elif card.provider_id == "custom" and not settings.custom_base_url:
            detail = _CUSTOM_NO_ADDRESS
        return ProviderCatalog(
            provider_id=card.provider_id,
            label=card.label,
            local=card.local,
            requires_key=card.requires_key,
            has_key=masked is not None,
            masked_key=masked,
            base_url=self._base_url(card.provider_id, settings, local),
            editable_base_url=card.editable_base_url,
            models=models,
            free_text_model=card.free_text_model,
            detail=key_detail if key_detail is not None else detail,
        )

    def _key_state(self, provider_id: ProviderId) -> tuple[str | None, str | None]:
        """`(masked key, why we cannot say)`. A broken vault is shown, never raised."""
        try:
            return self._vault.masked_key(provider_id), None
        except VaultError as error:
            return None, str(error)

    def _base_url(
        self,
        provider_id: ProviderId,
        settings: ModelSettings,
        local: _LocalModels,
    ) -> str | None:
        if provider_id == "custom":
            return settings.custom_base_url
        if provider_id == "ollama":
            return settings.ollama_base_url if settings.ollama_base_url else local.base_url
        return None

    async def _ollama_card(self, base_url: str | None) -> _LocalModels:
        """Ask the local server what it has. Absent is the ordinary answer, not a failure."""
        provider = await detect(base_url=base_url, transport=self._transport)
        if provider is None:
            return _LocalModels({}, _OLLAMA_ABSENT, base_url or OLLAMA_DEFAULT_BASE_URL)
        try:
            models = provider.models
            detail = None if models else _OLLAMA_EMPTY
            return _LocalModels(models, detail, provider.base_url)
        finally:
            await provider.aclose()

    # -- the Test button ---------------------------------------------------

    async def validate(self, provider_id: ProviderId) -> ValidateResponse:
        """One real call to the provider, timed. Never raises for a key that is wrong.

        A provider settings cannot describe — a `custom` gateway with no address — is a
        failed test with the message that says how to fix it, not an exception: the
        button must always answer.
        """
        started = time.perf_counter()
        try:
            provider = self._provider_pool().get(provider_id)
            status = await provider.validate_key()
            valid, detail = status.valid, status.detail
        except RouterError as error:
            valid, detail = False, str(error)
        except ProviderError as error:
            # `validate_key` is documented not to raise for a bad key; this is the
            # network being down, or a provider answering something unreadable.
            valid, detail = False, error.reason
        elapsed = int((time.perf_counter() - started) * 1000)
        log.info(
            "models.validated",
            extra={"provider_id": provider_id, "valid": valid, "latency_ms": elapsed},
        )
        return ValidateResponse(
            provider_id=provider_id, valid=valid, detail=detail, latency_ms=elapsed
        )

    # -- keys --------------------------------------------------------------

    def set_key(self, provider_id: ProviderId, key: str) -> str | None:
        """Save a key and answer with its masked form. Raises `VaultError`, which says
        what was wrong without quoting any part of the key."""
        self._vault.set_key(provider_id, key)
        return self._vault.masked_key(provider_id)

    def delete_key(self, provider_id: ProviderId) -> bool:
        return self._vault.delete_key(provider_id)

    def masked_key(self, provider_id: ProviderId) -> str | None:
        return self._vault.masked_key(provider_id)

    # -- spend -------------------------------------------------------------

    def spend(self) -> SpendResponse:
        """Today's total and the ceilings it is measured against."""
        limits = self.settings().limits
        day = self._ledger.day_spend()
        return SpendResponse(
            day=_totals_of(day.cents, day.tokens, day.unpriced_calls),
            limits=limits,
        )

    def guard(self) -> BudgetGuard:
        """The budget guard, with the user's ceilings. Built per call: cheap, and always
        current with the settings the user last saved."""
        return BudgetGuard(
            self._ledger,
            limits=budget_limits(self.settings().limits),
            publisher=self._publisher,
        )

    # -- routing -----------------------------------------------------------

    def router(self) -> ModelRouter | None:
        """A router for the saved role map, or `None` while a role is unmapped.

        `P4` is the caller. It shares the pool with the *Test* button because a provider
        instance owns a connection pool and a second one would pay a second handshake.
        """
        roles = role_map(self.settings())
        if roles is None:
            return None
        return ModelRouter(roles, self._provider_pool(), guard=self.guard())

    def _provider_pool(self) -> ProviderPool:
        if self._pool is None:
            settings = self.settings()
            self._pool = ProviderPool(
                self._vault,
                custom_base_url=settings.custom_base_url,
                ollama_base_url=settings.ollama_base_url or OLLAMA_DEFAULT_BASE_URL,
                transport=self._transport,
            )
        return self._pool

    async def _drop_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.aclose()

    async def aclose(self) -> None:
        """Release every connection pool and both database connections."""
        await self._drop_pool()
        self.close()

    def close(self) -> None:
        """Release the database connections. Safe to call more than once.

        Separate from `aclose()` because the two halves die in different places. The
        provider pools belong to the event loop and are released by the app's lifespan;
        the connections belong to the process, and `__main__.py` has to be able to
        release them on a path where the server never started and so there is no loop to
        await on.
        """
        self._settings_store.close()
        self._ledger.close()


class _LocalModels:
    """What the local server answered, so `_card` does not have to ask twice."""

    def __init__(
        self,
        models: Mapping[str, Capabilities],
        detail: str | None,
        base_url: str,
    ) -> None:
        self.models = models
        self.detail = detail
        self.base_url = base_url


def _checked_url(base_url: str | None) -> str | None:
    """A saved address, normalised, or `None`.

    `normalise_base_url` is what decides where a key may be sent (`P1-05`): `https`
    anywhere, `http` only to this machine, no credentials, query or fragment. Running it
    at save time means the user is told immediately, in the field they typed it in,
    rather than at the first call. `ProviderPool` runs it again when it builds an
    adapter, because a document can also be written by an older build.
    """
    if not base_url or not base_url.strip():
        return None
    return normalise_base_url(base_url)


__all__ = [
    "CARDS",
    "SETTINGS_KEY",
    "ModelService",
    "ProviderCard",
    "budget_limits",
    "role_map",
]
