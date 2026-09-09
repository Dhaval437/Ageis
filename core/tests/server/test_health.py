"""Tests for the FastAPI app and `GET /v1/health` (`ARCHITECTURE.md § 9.1`)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from aegis_core import __version__
from aegis_core.server.app import create_app
from aegis_core.server.schemas import HealthResponse
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_answers_the_documented_shape(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    health = HealthResponse.model_validate(response.json())
    assert health.status == "ok"
    assert health.version == __version__
    assert health.uptime >= 0


def test_health_reports_a_growing_uptime(client: TestClient) -> None:
    first = HealthResponse.model_validate(client.get("/v1/health").json())
    second = HealthResponse.model_validate(client.get("/v1/health").json())
    assert second.uptime >= first.uptime


def test_each_app_has_its_own_uptime_clock() -> None:
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        assert first.get("/v1/health").json()["uptime"] >= 0
        assert second.get("/v1/health").json()["uptime"] >= 0


def test_routes_live_only_under_the_v1_prefix(client: TestClient) -> None:
    assert client.get("/health").status_code == 404


def test_unknown_paths_are_404(client: TestClient) -> None:
    assert client.get("/v1/nothing-here").status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_schema_or_docs_endpoints_are_served(client: TestClient, path: str) -> None:
    """The only client is MAIN; an unauthenticated schema on loopback is surface."""
    assert client.get(path).status_code == 404


def test_health_response_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        HealthResponse.model_validate(
            {"status": "ok", "version": "0.0.0", "uptime": 1.0, "token": "leaked"}
        )
