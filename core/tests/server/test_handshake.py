"""Tests for the core's half of the startup handshake (`ARCHITECTURE.md § 3.1`)."""

from __future__ import annotations

import io
import json
import socket

import pytest
from aegis_core.server.handshake import (
    LOOPBACK,
    Handshake,
    HandshakeError,
    announce,
    bind_loopback,
    read_token,
)


class _UnreadableStream(io.StringIO):
    # typeshed types `_IOBase.readline` as returning bytes; `StringIO` narrows it
    # to `str` without re-declaring, so an override here cannot satisfy both.
    def readline(self, size: int = -1, /) -> str:  # type: ignore[override]
        raise OSError("the pipe went away")


class _UnwritableStream(io.StringIO):
    def write(self, text: str, /) -> int:
        raise OSError("the pipe went away")


class _RecordingStream(io.StringIO):
    """Captures what was on the stream at the moment it was closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_with: str | None = None

    def close(self) -> None:
        self.closed_with = self.getvalue()
        super().close()


# --------------------------------------------------------------------------- #
# The token comes off stdin, never off argv
# --------------------------------------------------------------------------- #


def test_reads_the_token_from_the_pipe() -> None:
    assert read_token(io.StringIO("deadbeef\n")) == "deadbeef"


def test_a_token_without_a_newline_still_reads() -> None:
    assert read_token(io.StringIO("deadbeef")) == "deadbeef"


def test_only_the_first_line_is_the_token() -> None:
    assert read_token(io.StringIO("deadbeef\nnot-the-token\n")) == "deadbeef"


@pytest.mark.parametrize("payload", ["", "\n", "   \n", "\t"])
def test_a_missing_token_is_fatal(payload: str) -> None:
    """An open port with no credential is worse than no core at all."""
    with pytest.raises(HandshakeError, match="no session token"):
        read_token(io.StringIO(payload))


def test_an_oversized_token_is_fatal() -> None:
    with pytest.raises(HandshakeError, match="too long"):
        read_token(io.StringIO("x" * 1000 + "\n"))


def test_an_unreadable_pipe_is_fatal() -> None:
    with pytest.raises(HandshakeError, match="could not read"):
        read_token(_UnreadableStream())


# --------------------------------------------------------------------------- #
# The socket is bound before the server starts, so the port can be announced
# --------------------------------------------------------------------------- #


def test_binds_an_ephemeral_loopback_port() -> None:
    sock = bind_loopback(0)
    try:
        host, port = sock.getsockname()
        assert host == LOOPBACK
        assert 0 < port < 65536
    finally:
        sock.close()


def test_the_bound_socket_accepts_a_loopback_connection() -> None:
    sock = bind_loopback(0)
    try:
        port = sock.getsockname()[1]
        with socket.create_connection((LOOPBACK, port), timeout=2):
            pass
    finally:
        sock.close()


def test_a_port_already_in_use_is_fatal() -> None:
    first = bind_loopback(0)
    try:
        with pytest.raises(HandshakeError, match="could not bind"):
            bind_loopback(first.getsockname()[1])
    finally:
        first.close()


def test_a_failed_bind_leaks_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bind_loopback` closes its own socket on the failure path."""
    closed: list[bool] = []

    class _Refusing:
        def bind(self, address: tuple[str, int]) -> None:
            raise OSError("refused")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("aegis_core.server.handshake.socket.socket", lambda *args: _Refusing())
    with pytest.raises(HandshakeError, match="could not bind"):
        bind_loopback(0)
    assert closed == [True]


# --------------------------------------------------------------------------- #
# One JSON line on stdout, then stdout is closed
# --------------------------------------------------------------------------- #


def test_the_line_is_exactly_the_documented_shape() -> None:
    line = Handshake(port=54321, pid=999, version="1.2.3").to_line()
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == {"port": 54321, "pid": 999, "version": "1.2.3"}


def test_announce_writes_one_line_and_closes_stdout() -> None:
    stream = _RecordingStream()
    announce(Handshake(port=1, pid=2, version="3"), stream)
    assert stream.closed_with == '{"port":1,"pid":2,"version":"3"}\n'
    assert stream.closed


def test_announce_closes_stdout_even_when_the_write_fails() -> None:
    """MAIN is blocked on a read; it must get an EOF rather than hang forever."""
    stream = _UnwritableStream()
    with pytest.raises(HandshakeError, match="could not write"):
        announce(Handshake(port=1, pid=2, version="3"), stream)
    assert stream.closed
