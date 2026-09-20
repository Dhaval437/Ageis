"""The Models screen's service (`P1-10`, `models/service.py`).

Three things this file is really about.

**A key goes in and never comes out.** Every answer the service gives is searched for
the key that was saved; the only representation allowed out is `mask_key()`'s.

**The catalogue works offline.** Nothing in `catalog()` may reach a vendor — the Models
screen is the screen you open when nothing works, and on a machine with no internet it
still has to list what the user can choose.

**A configuration that cannot run is refused where the user is.** A chain that repeats a
model, or a gateway address a key may not be sent to, is a `PUT` that fails with a
sentence, not a task that dies at its first step.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from aegis_core.models.budget import BudgetLimits
from aegis_core.models.service import (
    CARDS,
    SETTINGS_KEY,
    ModelService,
    budget_limits,
    role_map,
)
from aegis_core.server.schemas import (
    BudgetLimitsSpec,
    ModelCatalog,
    ModelChoiceSpec,
    ModelSettings,
    ProviderCatalog,
    RoleRouteSpec,
)
from aegis_core.storage.db import bootstrap
from aegis_core.storage.settings import SettingsStore
from aegis_core.storage.usage import UsageEntry, UsageLedger
from aegis_core.storage.vault import KeyVault, VaultError

from tests.storage.test_vault import FAKE_KEY, MemoryStore

A_CHOICE = ModelChoiceSpec(provider_id="openai", model="gpt-4o")


def a_settings(**overrides: object) -> ModelSettings:
    """A fully mapped configuration, so a test can change one thing about it."""
    base: dict[str, object] = {
        "planner": RoleRouteSpec(primary=A_CHOICE),
        "grounder": RoleRouteSpec(primary=ModelChoiceSpec(provider_id="openai", model="o3")),
        "utility": RoleRouteSpec(
            primary=ModelChoiceSpec(provider_id="openai", model="gpt-4o-mini")
        ),
    }
    return ModelSettings.model_validate(base | overrides)


class Loopback:
    """A transport that answers only what a machine on its own would answer.

    `ollama` gets a real reply; anything else raises, which is the point — a `catalog()`
    that reached a vendor would fail here rather than quietly costing a user their
    privacy and a screen that cannot open offline.
    """

    def __init__(self, models: list[str] | None = None, *, up: bool = True) -> None:
        self.models = [] if models is None else models
        self.up = up
        self.urls: list[str] = []

    def transport(self) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.urls.append(str(request.url))
            if request.url.host not in {"127.0.0.1", "localhost"}:
                raise AssertionError(f"the catalogue reached {request.url.host}")
            if not self.up:
                raise httpx2.ConnectError("connection refused")
            if request.url.path == "/api/tags":
                return httpx2.Response(
                    200, json={"models": [{"name": name} for name in self.models]}
                )
            if request.url.path == "/api/show":
                return httpx2.Response(200, json={"capabilities": ["completion", "tools"]})
            return httpx2.Response(404, json={})

        return httpx2.MockTransport(handler)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "Dhaval's data ü" / "aegis.db"


@pytest.fixture
def service_factory(db_path: Path) -> Iterator[Callable[..., ModelService]]:
    bootstrap(db_path)
    built: list[ModelService] = []

    def build(loopback: Loopback | None = None, vault: KeyVault | None = None) -> ModelService:
        local = Loopback() if loopback is None else loopback
        service = ModelService(
            SettingsStore.open(db_path),
            vault if vault is not None else KeyVault(MemoryStore()),
            UsageLedger.open(db_path),
            transport=local.transport(),
        )
        built.append(service)
        return service

    yield build
    for service in built:
        service._settings_store.close()
        service._ledger.close()


@pytest.fixture
def service(service_factory: Callable[..., ModelService]) -> ModelService:
    return service_factory()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_nothing_configured_answers_the_defaults(service: ModelService) -> None:
    settings = service.settings()
    assert settings.planner is None
    assert settings.grounder is None
    assert settings.utility is None
    assert settings.custom_base_url is None


def test_the_default_ceilings_are_the_budget_guards_own() -> None:
    """A second copy of a number is a second copy that drifts (`storage/usage.py`)."""
    assert budget_limits(BudgetLimitsSpec()) == BudgetLimits()


async def test_settings_round_trip_through_the_database(service: ModelService) -> None:
    saved = await service.save_settings(a_settings())
    assert saved.planner is not None
    assert saved.planner.primary.model == "gpt-4o"
    assert service.settings() == saved


async def test_a_partly_mapped_configuration_is_saveable(service: ModelService) -> None:
    """The state the screen is in halfway through being filled in."""
    settings = ModelSettings(planner=RoleRouteSpec(primary=A_CHOICE))
    saved = await service.save_settings(settings)
    assert saved.planner is not None
    assert saved.grounder is None
    assert role_map(saved) is None


async def test_every_role_mapped_gives_a_router(service: ModelService) -> None:
    await service.save_settings(a_settings())
    router = service.router()
    assert router is not None
    assert router.roles.route("planner").primary.model == "gpt-4o"
    await router.aclose()


def test_no_router_until_every_role_is_mapped(service: ModelService) -> None:
    assert service.router() is None


async def test_a_chain_that_repeats_a_model_is_refused(service: ModelService) -> None:
    """`RoleRoute`'s own rule, enforced at the save rather than at the first task."""
    doubled = RoleRouteSpec(primary=A_CHOICE, fallbacks=[A_CHOICE])
    with pytest.raises(ValueError, match="same model more than once"):
        await service.save_settings(a_settings(planner=doubled))
    assert service.settings().planner is None


async def test_a_half_mapped_configuration_still_validates_the_roles_it_has(
    service: ModelService,
) -> None:
    """`role_map` answers `None` while a role is unset; the mapped ones are still checked."""
    doubled = RoleRouteSpec(primary=A_CHOICE, fallbacks=[A_CHOICE])
    with pytest.raises(ValueError, match="same model more than once"):
        await service.save_settings(ModelSettings(planner=doubled))


async def test_a_plaintext_address_off_this_machine_is_refused(service: ModelService) -> None:
    """`P1-05`: that URL decides where the user's key is sent."""
    with pytest.raises(ValueError, match="https://"):
        await service.save_settings(a_settings(custom_base_url="http://example.com/v1"))


async def test_a_plaintext_loopback_gateway_is_allowed(service: ModelService) -> None:
    saved = await service.save_settings(a_settings(custom_base_url="http://127.0.0.1:8000/v1"))
    assert saved.custom_base_url == "http://127.0.0.1:8000/v1"


async def test_a_blank_address_is_stored_as_no_address(service: ModelService) -> None:
    saved = await service.save_settings(a_settings(custom_base_url="   "))
    assert saved.custom_base_url is None


async def test_an_unreadable_settings_document_reads_as_unconfigured(
    service: ModelService,
) -> None:
    """The screen that could fix a bad document has to open."""
    service._settings_store.put(SETTINGS_KEY, {"planner": "not a route"})
    assert service.settings().planner is None


async def test_a_document_without_limits_keeps_the_default_ceilings(
    service: ModelService,
) -> None:
    """An older build's document must not silently remove every ceiling."""
    service._settings_store.put(SETTINGS_KEY, {"planner": None})
    assert service.settings().limits == BudgetLimitsSpec()
    assert service.settings().limits.day_cents == 1_000.0


async def test_an_explicit_null_ceiling_means_no_ceiling(service: ModelService) -> None:
    saved = await service.save_settings(
        a_settings(limits=BudgetLimitsSpec(task_cents=None, day_cents=50.0))
    )
    assert saved.limits.task_cents is None
    assert budget_limits(saved.limits).day_cents == 50.0


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def by_id(catalog: ModelCatalog, provider_id: str) -> ProviderCatalog:
    return next(card for card in catalog.providers if card.provider_id == provider_id)


async def test_the_catalogue_lists_every_provider(service: ModelService) -> None:
    catalog = await service.catalog()
    assert [card.provider_id for card in catalog.providers] == [
        card.provider_id for card in CARDS
    ]


async def test_the_catalogue_reaches_no_vendor(
    service_factory: Callable[..., ModelService],
) -> None:
    """The transport raises for any host but loopback, so this fails loudly if it ever does."""
    loopback = Loopback(["llama3.1:8b"])
    catalog = await service_factory(loopback).catalog()
    assert catalog.providers
    assert all("127.0.0.1" in url or "localhost" in url for url in loopback.urls)


async def test_openai_is_listed_with_prices_and_no_free_text(service: ModelService) -> None:
    card = by_id(await service.catalog(), "openai")
    assert not card.free_text_model
    assert card.requires_key
    names = [model.id for model in card.models]
    assert "gpt-4o" in names
    assert next(m for m in card.models if m.id == "gpt-4o").cost_per_mtok_input == 2.50


async def test_a_gateway_this_build_cannot_enumerate_asks_the_user_to_type_one(
    service: ModelService,
) -> None:
    card = by_id(await service.catalog(), "openrouter")
    assert card.free_text_model
    assert card.models == []
    assert card.detail is not None


async def test_ollama_lists_what_the_local_server_has(
    service_factory: Callable[..., ModelService],
) -> None:
    card = by_id(await service_factory(Loopback(["llama3.1:8b", "qwen:7b"])).catalog(), "ollama")
    assert [model.id for model in card.models] == ["llama3.1:8b", "qwen:7b"]
    assert card.local
    assert not card.requires_key
    assert card.detail is None


async def test_a_local_model_is_priced_at_zero_not_unknown(
    service_factory: Callable[..., ModelService],
) -> None:
    """`P1-06`: free is a fact on this machine, and `None` would pause a free task."""
    card = by_id(await service_factory(Loopback(["llama3.1:8b"])).catalog(), "ollama")
    assert card.models[0].cost_per_mtok_input == 0.0


async def test_ollama_absent_is_an_ordinary_answer(
    service_factory: Callable[..., ModelService],
) -> None:
    card = by_id(await service_factory(Loopback(up=False)).catalog(), "ollama")
    assert card.models == []
    assert card.detail is not None
    assert "Ollama isn't running" in card.detail


async def test_ollama_running_with_nothing_pulled_says_so(
    service_factory: Callable[..., ModelService],
) -> None:
    card = by_id(await service_factory(Loopback([])).catalog(), "ollama")
    assert card.detail is not None
    assert "no models are installed" in card.detail


async def test_a_custom_gateway_with_no_address_says_what_to_do(service: ModelService) -> None:
    card = by_id(await service.catalog(), "custom")
    assert card.editable_base_url
    assert card.base_url is None
    assert card.detail == "Add the address of your gateway, then Test it."


async def test_a_saved_gateway_address_is_shown_back(service: ModelService) -> None:
    await service.save_settings(a_settings(custom_base_url="https://gateway.example/v1"))
    card = by_id(await service.catalog(), "custom")
    assert card.base_url == "https://gateway.example/v1"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


async def test_a_saved_key_is_reported_masked_and_never_whole(service: ModelService) -> None:
    service.set_key("openai", FAKE_KEY)
    card = by_id(await service.catalog(), "openai")
    assert card.has_key
    assert card.masked_key == "sk-…mnop"
    assert FAKE_KEY not in json.dumps((await service.catalog()).model_dump(mode="json"))


async def test_no_answer_the_service_gives_contains_a_key(service: ModelService) -> None:
    """The whole surface, searched. `ARCHITECTURE.md § 5.3`, invariant 9."""
    service.set_key("openai", FAKE_KEY)
    await service.save_settings(a_settings())
    answers = [
        (await service.catalog()).model_dump(mode="json"),
        service.settings().model_dump(mode="json"),
        service.spend().model_dump(mode="json"),
    ]
    blob = json.dumps(answers)
    assert FAKE_KEY not in blob
    assert FAKE_KEY[4:20] not in blob


def test_removing_a_key_leaves_the_provider_without_one(service: ModelService) -> None:
    service.set_key("openai", FAKE_KEY)
    assert service.delete_key("openai") is True
    assert service.masked_key("openai") is None
    assert service.delete_key("openai") is False


def test_a_key_with_a_newline_is_refused(service: ModelService) -> None:
    """A key goes straight into a header; a CR or LF in one is header injection."""
    with pytest.raises(VaultError, match="characters an API key cannot contain"):
        service.set_key("openai", f"{FAKE_KEY}\nX-Evil: 1")


# ---------------------------------------------------------------------------
# The Test button
# ---------------------------------------------------------------------------


async def test_testing_a_gateway_with_no_address_fails_with_the_fix(
    service: ModelService,
) -> None:
    """`ProviderUnavailableError` is settings being wrong, and the button must answer."""
    result = await service.validate("custom")
    assert not result.valid
    assert "custom gateway" in result.detail
    assert result.latency_ms >= 0


async def test_testing_ollama_reports_what_it_found(
    service_factory: Callable[..., ModelService],
) -> None:
    result = await service_factory(Loopback(["llama3.1:8b"])).validate("ollama")
    assert result.valid
    assert "1 model" in result.detail


async def test_testing_ollama_while_it_is_down_names_the_fix(
    service_factory: Callable[..., ModelService],
) -> None:
    result = await service_factory(Loopback(up=False)).validate("ollama")
    assert not result.valid
    assert "Check that it is running" in result.detail


async def test_a_failed_test_never_quotes_the_key(
    service_factory: Callable[..., ModelService],
) -> None:
    service = service_factory(Loopback(up=False))
    service.set_key("ollama", FAKE_KEY)
    result = await service.validate("ollama")
    assert FAKE_KEY not in result.detail


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


def test_spend_starts_at_zero_against_the_saved_ceilings(service: ModelService) -> None:
    spend = service.spend()
    assert spend.day.cents == 0.0
    assert spend.limits.day_cents == 1_000.0


def test_spend_totals_what_the_ledger_holds(service: ModelService) -> None:
    service._ledger.record(
        UsageEntry(
            role="planner",
            provider_id="openai",
            model="gpt-4o",
            input_tokens=1000,
            output_tokens=200,
            cost_cents=0.45,
        )
    )
    spend = service.spend()
    assert spend.day.cents == pytest.approx(0.45)
    assert spend.day.tokens == 1200
    assert spend.day.unpriced_calls == 0


def test_an_unpriced_call_is_counted_but_never_summed_as_zero(service: ModelService) -> None:
    """`P1-09`: a total that is short has to say so."""
    service._ledger.record(
        UsageEntry(
            role="utility",
            provider_id="custom",
            model="whatever",
            input_tokens=10,
            output_tokens=5,
            cost_cents=None,
        )
    )
    spend = service.spend()
    assert spend.day.cents == 0.0
    assert spend.day.unpriced_calls == 1


async def test_spend_follows_the_ceilings_the_user_saved(service: ModelService) -> None:
    await service.save_settings(a_settings(limits=BudgetLimitsSpec(day_cents=25.0)))
    assert service.spend().limits.day_cents == 25.0
    assert service.guard().limits.day_cents == 25.0
