"""Tests for session authentication (`ARCHITECTURE.md § 3.1` step 5, `§ 8.6`).

The peer-PID check is driven through an injected resolver so the assertions are
about the *rule*, not about whatever the machine's connection table happens to
hold while the suite runs. `test_peer_lookup.py` covers the real resolver.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import psutil
import pytest
from aegis_core.parent_watch import GONE, UNIDENTIFIED, Identity
from aegis_core.server.app import create_app
from aegis_core.server.auth import (
    PeerLookupError,
    PeerResolver,
    ProcessIdentifier,
    SessionAuth,
)
from fastapi.testclient import TestClient

TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64

#: `TestClient` reports this peer; the resolver below maps it to a trusted PID.
TRUSTED_PEER_PORT = 50000
SUPERVISOR_PID = 4242

#: MAIN's creation time. Any float will do; what matters is that it is stable.
SUPERVISOR_STARTED_AT = 1_700_000_000.0


def _resolver(local_port: int, peer_port: int) -> int | None:
    """Every peer resolves to MAIN itself, so only the other checks can fail."""
    return SUPERVISOR_PID


def _identifier(pid: int) -> Identity:
    """`SUPERVISOR_PID` is alive and unchanged; every other PID is gone."""
    return SUPERVISOR_STARTED_AT if pid == SUPERVISOR_PID else GONE


def make_auth(
    *,
    token: str = TOKEN,
    supervisor_pid: int = SUPERVISOR_PID,
    resolve_peer: PeerResolver = _resolver,
    identify: ProcessIdentifier = _identifier,
) -> SessionAuth:
    return SessionAuth(
        token=token,
        supervisor_pid=supervisor_pid,
        local_port=1234,
        resolve_peer=resolve_peer,
        identify=identify,
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


# --------------------------------------------------------------------------- #
# The supervisor rule (P0-16): the peer must be MAIN, not a relative of MAIN
# --------------------------------------------------------------------------- #


def test_the_supervisor_itself_is_trusted() -> None:
    auth = SessionAuth(token=TOKEN, supervisor_pid=os.getpid(), local_port=1234)

    assert auth.is_the_supervisor(os.getpid()) is True


def test_a_job_the_core_spawns_cannot_call_the_core() -> None:
    """The whole point of P0-16, against real processes.

    This test process stands in for the core, its parent for MAIN, and the child
    below for a PowerShell or Playwright job the agent starts. That child really
    is inside MAIN's descendant chain — asserted here, so the test cannot quietly
    stop covering the case — and the old "child chain" rule would have admitted
    it, letting a compromised job drive the agent through the agent's own API.
    """
    main_pid = os.getppid()
    job = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ancestors = {parent.pid for parent in psutil.Process(job.pid).parents()}
        assert os.getpid() in ancestors, "the job must be below the core for this to mean anything"
        assert main_pid in ancestors, "the job must be below MAIN for this to mean anything"

        auth = SessionAuth(token=TOKEN, supervisor_pid=main_pid, local_port=1234)
        assert auth.is_the_supervisor(job.pid) is False
        assert auth.is_the_supervisor(os.getpid()) is False, "nor is the core itself"
    finally:
        job.kill()
        job.wait(timeout=30)


def test_an_unrelated_pid_is_never_the_supervisor() -> None:
    assert make_auth().is_the_supervisor(999999) is False


def test_a_supervisor_that_has_died_is_not_trusted() -> None:
    """Its PID is a free slot now, and a free slot is not MAIN."""
    assert make_auth(identify=lambda _pid: GONE).is_the_supervisor(SUPERVISOR_PID) is False


def test_a_recycled_pid_does_not_pass_for_the_supervisor() -> None:
    """Same PID, different creation time — Windows handed the number to someone else."""
    auth = make_auth()
    auth.identify = lambda _pid: SUPERVISOR_STARTED_AT + 1

    assert auth.is_the_supervisor(SUPERVISOR_PID) is False


def test_a_supervisor_the_os_will_not_describe_is_still_trusted_on_its_pid() -> None:
    """`AccessDenied` must not lock MAIN out of its own core (see `identities_match`)."""
    auth = make_auth(identify=lambda _pid: UNIDENTIFIED)

    assert auth.is_the_supervisor(SUPERVISOR_PID) is True
    assert auth.is_the_supervisor(SUPERVISOR_PID + 1) is False


def test_a_job_with_a_valid_token_gets_the_same_bare_401() -> None:
    """End to end through the middleware, not just the rule."""

    def a_job_the_core_spawned(local_port: int, peer_port: int) -> int | None:
        return SUPERVISOR_PID + 7

    with TestClient(create_app(make_auth(resolve_peer=a_job_the_core_spawned))) as client:
        response = client.get("/v1/health", headers=AUTHORIZED)

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
