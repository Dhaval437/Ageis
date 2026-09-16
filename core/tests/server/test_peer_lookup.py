"""The real peer resolver, against a real loopback connection.

`test_auth.py` proves the *rule*; this proves the lookup that feeds it actually
works on this machine. If `psutil` cannot read the connection table here, every
request would be denied — so this is the test that would tell us.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest
from aegis_core.server.auth import SessionAuth, resolve_peer_pid
from aegis_core.server.handshake import LOOPBACK, bind_loopback


@pytest.fixture
def connected() -> Iterator[tuple[int, int]]:
    """A live loopback connection, both ends in this process.

    Yields `(local_port, peer_port)` as the server side sees them.
    """
    listener = bind_loopback(0)
    client: socket.socket | None = None
    accepted: socket.socket | None = None
    try:
        local_port = listener.getsockname()[1]
        client = socket.create_connection((LOOPBACK, local_port), timeout=5)
        accepted, peer = listener.accept()
        yield (local_port, peer[1])
    finally:
        if accepted is not None:
            accepted.close()
        if client is not None:
            client.close()
        listener.close()


def test_resolves_the_connecting_process(connected: tuple[int, int]) -> None:
    local_port, peer_port = connected
    assert resolve_peer_pid(local_port, peer_port) == os.getpid()


def test_the_resolved_process_passes_the_supervision_rule(connected: tuple[int, int]) -> None:
    """End to end: this connection would be accepted by a core we supervise.

    Both ends are in this process, so the real resolver, the real connection
    table and the real PID-plus-creation-time identity all have to agree.
    """
    local_port, peer_port = connected
    pid = resolve_peer_pid(local_port, peer_port)
    auth = SessionAuth(token="t" * 64, supervisor_pid=os.getpid(), local_port=local_port)

    assert pid is not None
    assert auth.is_the_supervisor(pid) is True


def test_an_unconnected_peer_port_resolves_to_nothing(connected: tuple[int, int]) -> None:
    local_port, _peer_port = connected
    # Port 1 is never the local end of a connection to us.
    assert resolve_peer_pid(local_port, 1) is None
