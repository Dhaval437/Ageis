"""Tests for session authentication (`ARCHITECTURE.md § 3.1` step 5, `§ 8.6`).

The peer-PID check is driven through an injected resolver so the assertions are
about the *rule*, not about whatever the machine's connection table happens to
hold while the suite runs. `test_peer_lookup.py` covers the real resolver.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from aegis_core.server.app import create_app
from aegis_core.server.auth import (
    PeerLookupError,
    PeerResolver,
    SessionAuth,
    is_supervised_by,
)
from fastapi.testclient import TestClient

TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64

#: `TestClient` reports this peer; the resolver below maps it to a trusted PID.
TRUSTED_PEER_PORT = 50000
SUPERVISOR_PID = 4242


def _resolver(local_port: int, peer_port: int) -> int | None:
    """Every peer resolves to MAIN itself, so only the other checks can fail."""
    return SUPERVISOR_PID


def make_auth(
    *,
    token: str = TOKEN,
    supervisor_pid: int = SUPERVISOR_PID,
    resolve_peer: PeerResolver = _resolver,
) -> SessionAuth:
    return SessionAuth(
        token=token,
        supervisor_pid=supervisor_pid,
        local_port=1234,
        resolve_peer=resolve_peer,
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(make_auth())) as test_client:
        yield test_client


AUTHORIZED = {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- #
# End to end, through the middleware
# --------------------------------------------------------------------------- #


def test_a_correct_token_reaches_the_route(client: TestClient) -> None:
    response = client.get("/v1/health", headers=AUTHORIZED)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_no_token_is_401(client: TestClient) -> None:
    assert client.get("/v1/health").status_code == 401


def test_a_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/health", headers={"Authorization": f"Bearer {OTHER_TOKEN}"})
    assert response.status_code == 401


def test_an_origin_header_is_rejected_even_with_the_right_token(client: TestClient) -> None:
    """An `Origin` means a web page found the port. MAIN never sends one."""
    response = client.get("/v1/health", headers={**AUTHORIZED, "Origin": "http://localhost:5173"})
    assert response.status_code == 401


def test_the_401_body_never_says_which_check_failed(client: TestClient) -> None:
    bodies = {
        client.get("/v1/health").text,
        client.get("/v1/health", headers={"Authorization": "Bearer wrong"}).text,
        client.get("/v1/health", headers={**AUTHORIZED, "Origin": "http://x"}).text,
    }
    assert bodies == {'{"detail":"unauthorized"}\n'}


def test_the_401_body_never_contains_the_token(client: TestClient) -> None:
    response = client.get("/v1/health", headers={"Authorization": f"Bearer {OTHER_TOKEN}"})
    assert TOKEN not in response.text
    assert TOKEN not in str(dict(response.headers))


def test_unknown_routes_are_401_before_they_are_404(client: TestClient) -> None:
    """Otherwise an unauthenticated caller could map the API by status code."""
    assert client.get("/v1/nothing-here").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/v1/nothing-here", headers=AUTHORIZED).status_code == 404


def test_an_untrusted_peer_is_rejected_with_a_valid_token() -> None:
    def stranger(local_port: int, peer_port: int) -> int | None:
        return 999999

    auth = make_auth(resolve_peer=stranger)
    with TestClient(create_app(auth)) as client:
        assert client.get("/v1/health", headers=AUTHORIZED).status_code == 401


def test_an_unresolvable_peer_is_rejected() -> None:
    def nobody(local_port: int, peer_port: int) -> int | None:
        return None

    with TestClient(create_app(make_auth(resolve_peer=nobody))) as client:
        assert client.get("/v1/health", headers=AUTHORIZED).status_code == 401


def test_a_peer_lookup_failure_is_rejected_not_allowed() -> None:
    """If the OS will not say who is calling, the answer is no."""

    def refuses(local_port: int, peer_port: int) -> int | None:
        raise PeerLookupError("access denied")

    with TestClient(create_app(make_auth(resolve_peer=refuses))) as client:
        assert client.get("/v1/health", headers=AUTHORIZED).status_code == 401


# --------------------------------------------------------------------------- #
# Token comparison
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        TOKEN,  # no scheme
        f"Basic {TOKEN}",
        "Bearer",
        "Bearer ",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {'x' * 2000}",
    ],
)
def test_token_matches_rejects(header: str | None) -> None:
    assert make_auth().token_matches(header) is False


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}", f"bearer {TOKEN}", f"BEARER {TOKEN} "])
def test_token_matches_accepts(header: str) -> None:
    """The scheme is case-insensitive per RFC 7235; the token is not."""
    assert make_auth().token_matches(header) is True


# --------------------------------------------------------------------------- #
# Peer trust
# --------------------------------------------------------------------------- #


def test_a_missing_peer_is_never_trusted() -> None:
    assert make_auth().peer_is_trusted(None) is False


def test_the_peer_decision_is_cached_per_connection() -> None:
    calls: list[int] = []

    def counting(local_port: int, peer_port: int) -> int | None:
        calls.append(peer_port)
        return SUPERVISOR_PID

    auth = make_auth(resolve_peer=counting)
    for _ in range(5):
        assert auth.peer_is_trusted(("127.0.0.1", TRUSTED_PEER_PORT)) is True
    assert len(calls) == 1


def test_the_peer_cache_is_bounded() -> None:
    auth = make_auth()
    for port in range(1000, 1600):
        auth.peer_is_trusted(("127.0.0.1", port))
    assert len(auth._cache.entries) <= 256


def test_a_lookup_failure_is_not_cached() -> None:
    """A transient OS refusal must not pin a denial onto a peer that is ours."""
    attempts: list[int] = []

    def flaky(local_port: int, peer_port: int) -> int | None:
        attempts.append(peer_port)
        if len(attempts) == 1:
            raise PeerLookupError("transient")
        return SUPERVISOR_PID

    auth = make_auth(resolve_peer=flaky)
    peer = ("127.0.0.1", TRUSTED_PEER_PORT)
    assert auth.peer_is_trusted(peer) is False
    assert auth.peer_is_trusted(peer) is True


def test_this_process_is_supervised_by_itself() -> None:
    assert is_supervised_by(os.getpid(), os.getpid()) is True


def test_this_process_is_supervised_by_its_parent() -> None:
    assert is_supervised_by(os.getpid(), os.getppid()) is True


def test_an_unrelated_pid_is_not_supervised() -> None:
    """PID 0 is never anyone's ancestor on Windows."""
    assert is_supervised_by(os.getpid(), 0) is False


def test_a_dead_pid_is_not_supervised() -> None:
    assert is_supervised_by(0x7FFFFFFF, os.getpid()) is False
