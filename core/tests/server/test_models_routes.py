"""The Models screen's routes (`P1-10`, `ARCHITECTURE.md § 9.1`).

The service itself is tested in `tests/models/test_service.py`. What is tested here is
the shell around it, which exists for three reasons and is checked against each:

* a key goes **in** through exactly one route and comes back out of none;
* a refusal the user can act on is a `400` carrying the sentence they need, and a
  malformed request is a `422` that repeats **none** of what was sent;
* a core served without a model layer answers `503`, not `500`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from aegis_core.models.service import ModelService
from aegis_core.server.app import create_app
from aegis_core.server.schemas import ModelCatalog, SettingsResponse, SpendResponse
from aegis_core.storage.db import bootstrap
from aegis_core.storage.settings import SettingsStore
from aegis_core.storage.usage import UsageLedger
from aegis_core.storage.vault import KeyVault
from fastapi.testclient import TestClient

from tests.models.test_service import Loopback, a_settings
from tests.server.test_auth import AUTHORIZED, make_auth
from tests.storage.test_vault import FAKE_KEY, MemoryStore


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ModelService]:
    db_path = tmp_path / "aegis.db"
    bootstrap(db_path)
    built = ModelService(
        SettingsStore.open(db_path),
        KeyVault(MemoryStore()),
        UsageLedger.open(db_path),
        transport=Loopback(["llama3.1:8b"]).transport(),
    )
    yield built
    built._settings_store.close()
    built._ledger.close()


def _client(models: ModelService | None) -> TestClient:
    client = TestClient(create_app(make_auth(), None, models))
    client.headers.update(AUTHORIZED)
    return client


@pytest.fixture
def client(service: ModelService) -> Iterator[TestClient]:
    with _client(service) as test_client:
        yield test_client


@pytest.fixture
def bare_client() -> Iterator[TestClient]:
    """A core with no model layer — the shape every other server test runs in."""
    with _client(None) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Catalogue and spend
# ---------------------------------------------------------------------------


def test_the_catalogue_answers_the_documented_shape(client: TestClient) -> None:
    response = client.get("/v1/models/catalog")
    assert response.status_code == 200
    catalog = ModelCatalog.model_validate(response.json())
    assert next(card.provider_id for card in catalog.providers) == "openai"


def test_spend_answers_the_documented_shape(client: TestClient) -> None:
    response = client.get("/v1/models/spend")
    assert response.status_code == 200
    spend = SpendResponse.model_validate(response.json())
    assert spend.day.cents == 0.0
    assert spend.limits.day_cents == 1_000.0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_round_trip_over_http(client: TestClient) -> None:
    body = {"models": a_settings().model_dump(mode="json")}
    put = client.put("/v1/settings", json=body)
    assert put.status_code == 200
    assert SettingsResponse.model_validate(put.json()).models.planner is not None
    assert client.get("/v1/settings").json() == put.json()


def test_a_configuration_that_cannot_run_is_a_400_with_one_sentence(
    client: TestClient,
) -> None:
    """Pydantic's own string is a multi-line report with a URL in it; a user gets a line."""
    choice = {"provider_id": "openai", "model": "gpt-4o"}
    settings = a_settings().model_dump(mode="json")
    settings["planner"] = {"primary": choice, "fallbacks": [choice]}
    response = client.put("/v1/settings", json={"models": settings})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == "a role names the same model more than once"


def test_an_unusable_gateway_address_is_a_400_that_never_quotes_it(
    client: TestClient,
) -> None:
    settings = a_settings().model_dump(mode="json")
    settings["custom_base_url"] = "http://secret-host.example/v1"
    response = client.put("/v1/settings", json={"models": settings})
    assert response.status_code == 400
    assert "secret-host.example" not in response.text


def test_a_malformed_settings_body_is_a_422_that_echoes_nothing(client: TestClient) -> None:
    """The renderer displays text the agent scraped off the user's screen."""
    response = client.put(
        "/v1/settings",
        json={"models": {"planner": "a secret the renderer scraped", "limits": 7}},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Aegis could not read that request."}
    assert "scraped" not in response.text


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_saving_a_key_answers_with_the_mask_and_never_the_key(client: TestClient) -> None:
    response = client.put("/v1/models/keys/openai", json={"key": FAKE_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "provider_id": "openai",
        "has_key": True,
        "masked_key": "sk-…mnop",
    }
    assert FAKE_KEY not in response.text


def test_a_saved_key_is_never_readable_back_over_http(client: TestClient) -> None:
    client.put("/v1/models/keys/openai", json={"key": FAKE_KEY})
    everything = json.dumps(
        [
            client.get("/v1/models/catalog").json(),
            client.get("/v1/settings").json(),
            client.get("/v1/models/spend").json(),
        ]
    )
    assert FAKE_KEY not in everything
    assert FAKE_KEY[4:20] not in everything


def test_there_is_no_route_that_reads_a_key(client: TestClient) -> None:
    assert client.get("/v1/models/keys/openai").status_code == 405


def test_removing_a_key_says_there_is_none(client: TestClient) -> None:
    client.put("/v1/models/keys/openai", json={"key": FAKE_KEY})
    response = client.delete("/v1/models/keys/openai")
    assert response.status_code == 200
    assert response.json()["has_key"] is False
    assert client.get("/v1/models/catalog").json()["providers"][0]["has_key"] is False


def test_a_key_the_vault_refuses_is_a_400_that_quotes_none_of_it(client: TestClient) -> None:
    bad = f"{FAKE_KEY}\r\nX-Injected: 1"
    response = client.put("/v1/models/keys/openai", json={"key": bad})
    assert response.status_code == 400
    assert "X-Injected" not in response.text
    assert FAKE_KEY not in response.text


def test_an_unknown_provider_id_is_refused_by_the_path(client: TestClient) -> None:
    assert client.put("/v1/models/keys/not-a-provider", json={"key": FAKE_KEY}).status_code == 422


# ---------------------------------------------------------------------------
# The Test button
# ---------------------------------------------------------------------------


def test_validate_answers_rather_than_raising_for_a_provider_that_is_not_set_up(
    client: TestClient,
) -> None:
    response = client.post("/v1/models/validate", json={"provider_id": "custom"})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["latency_ms"] >= 0


def test_validate_reaches_the_local_server(client: TestClient) -> None:
    body = client.post("/v1/models/validate", json={"provider_id": "ollama"}).json()
    assert body["valid"] is True
    assert "1 model" in body["detail"]


# ---------------------------------------------------------------------------
# A core with no model layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call"),
    [
        lambda c: c.get("/v1/models/catalog"),
        lambda c: c.get("/v1/models/spend"),
        lambda c: c.get("/v1/settings"),
        lambda c: c.post("/v1/models/validate", json={"provider_id": "openai"}),
        lambda c: c.put("/v1/models/keys/openai", json={"key": FAKE_KEY}),
        lambda c: c.delete("/v1/models/keys/openai"),
    ],
)
def test_every_model_route_answers_503_without_a_service(
    bare_client: TestClient, call: Callable[[TestClient], httpx2.Response]
) -> None:
    """A subsystem that is not there says so (`P0-04`); it does not 500."""
    assert call(bare_client).status_code == 503


def test_health_still_works_without_a_model_layer(bare_client: TestClient) -> None:
    assert bare_client.get("/v1/health").status_code == 200
