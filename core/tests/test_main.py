"""Tests for the sidecar entry point and the handshake it performs on startup."""

from __future__ import annotations

import io
import json
import logging
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest
from aegis_core import __main__ as entry
from aegis_core import __version__
from aegis_core.perception.display import DpiAwareness, current_dpi_awareness
from aegis_core.server import handshake as handshake_module
from aegis_core.server.auth import SessionAuth
from aegis_core.server.handshake import LOOPBACK, Handshake, HandshakeError
from aegis_core.storage.db import StorageError
from fastapi import FastAPI

TOKEN = "f" * 64


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep `main()`'s `configure_logging()` inside the test's own directory."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)


class _Served:
    """What `_serve` was handed, captured instead of actually serving."""

    def __init__(self) -> None:
        self.app: FastAPI | None = None
        self.host: str | None = None
        self.port: int | None = None
        self.supervisor_pid: int | None = None

    def __call__(self, app: FastAPI, sock: socket.socket, *, supervisor_pid: int) -> None:
        # Read the address here: `main()` closes the socket on its way out.
        self.app = app
        self.host, self.port = sock.getsockname()
        self.supervisor_pid = supervisor_pid


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> _Served:
    capture = _Served()
    monkeypatch.setattr(entry, "_serve", capture)
    return capture


class _Pipe(io.StringIO):
    """Stands in for the stdout pipe MAIN reads; remembers what it held at close."""

    def __init__(self) -> None:
        super().__init__()
        self.written = ""

    def close(self) -> None:
        if not self.closed:
            self.written = self.getvalue()
        super().close()


def run(
    argv: list[str] | None = None,
    *,
    token: str = TOKEN,
) -> tuple[_Pipe, dict[str, object]]:
    """Run `main()` with piped stdio and return the stdout pipe plus the line."""
    stdout = _Pipe()
    entry.main(argv or [], stdin=io.StringIO(f"{token}\n"), stdout=stdout)
    parsed: dict[str, object] = json.loads(stdout.written)
    return stdout, parsed


# --------------------------------------------------------------------------- #
# The handshake (ARCHITECTURE.md § 3.1)
# --------------------------------------------------------------------------- #


def test_writes_exactly_one_handshake_line_on_stdout(served: _Served) -> None:
    stdout, line = run()
    assert stdout.written.count("\n") == 1
    assert set(line) == {"port", "pid", "version"}
    assert line["version"] == __version__
    assert line["pid"] == os.getpid()


def test_the_announced_port_is_the_port_actually_bound(served: _Served) -> None:
    _, line = run()
    assert line["port"] == served.port
    assert isinstance(line["port"], int)


def test_an_ephemeral_port_is_the_default(served: _Served) -> None:
    _, line = run()
    port = line["port"]
    assert isinstance(port, int)
    assert 0 < port < 65536


def test_an_explicit_port_is_honoured(served: _Served) -> None:
    # Take an ephemeral port, release it, then ask for it by number.
    probe = socket.socket()
    probe.bind((LOOPBACK, 0))
    wanted = probe.getsockname()[1]
    probe.close()

    _, line = run(["--port", str(wanted)])
    assert line["port"] == wanted


def test_the_socket_is_bound_to_loopback_only(served: _Served) -> None:
    run()
    assert served.host == LOOPBACK


def test_stdout_is_closed_after_the_handshake(served: _Served) -> None:
    stdout, _ = run()
    assert stdout.closed


def test_no_token_means_no_server(served: _Served) -> None:
    """A bound port with no credential must never exist."""
    with pytest.raises(HandshakeError):
        entry.main([], stdin=io.StringIO("\n"), stdout=io.StringIO())
    assert served.app is None


def test_a_missing_token_leaves_nothing_listening(served: _Served) -> None:
    with pytest.raises(HandshakeError):
        entry.main([], stdin=io.StringIO(""), stdout=io.StringIO())
    assert served.port is None


# --------------------------------------------------------------------------- #
# What the server is started with
# --------------------------------------------------------------------------- #


def _auth_of(app: FastAPI) -> SessionAuth:
    for middleware in app.user_middleware:
        auth = middleware.kwargs.get("auth")
        if isinstance(auth, SessionAuth):
            return auth
    raise AssertionError("the app was built without SessionAuthMiddleware")


def test_the_app_is_authenticated_with_the_piped_token(served: _Served) -> None:
    run()
    assert served.app is not None
    assert _auth_of(served.app).token == TOKEN


def test_the_auth_knows_the_port_it_is_bound_to(served: _Served) -> None:
    """The peer lookup matches on that port; a zero here would match nothing."""
    _, line = run()
    assert served.app is not None
    assert _auth_of(served.app).local_port == line["port"]


def test_the_supervisor_defaults_to_the_parent_process(served: _Served) -> None:
    run()
    assert served.app is not None
    assert _auth_of(served.app).supervisor_pid == os.getppid()


def test_the_supervisor_pid_can_be_given_explicitly(served: _Served) -> None:
    run(["--supervisor-pid", "1234"])
    assert served.app is not None
    assert _auth_of(served.app).supervisor_pid == 1234


def test_the_server_is_told_who_supervises_it(served: _Served) -> None:
    """The parent watch needs the same PID the auth check uses, or it watches nothing."""
    run(["--supervisor-pid", "1234"])
    assert served.supervisor_pid == 1234


def test_the_token_is_never_written_to_stdout(served: _Served) -> None:
    stdout, _ = run()
    assert TOKEN not in stdout.written


def test_the_token_is_never_written_to_the_log(served: _Served, tmp_path: Path) -> None:
    """`REMEMBER.md` invariant 9: a session token in a log file is a leaked token."""
    run()
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = (tmp_path / "Aegis" / "logs" / "core.log").read_text(encoding="utf-8")
    assert TOKEN not in contents


def test_main_logs_a_startup_line_to_file(served: _Served, tmp_path: Path) -> None:
    run()
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = (tmp_path / "Aegis" / "logs" / "core.log").read_text(encoding="utf-8")
    assert "core.starting" in contents
    assert "core.listening" in contents


def test_the_database_is_ready_before_the_core_announces(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "Aegis" / "aegis.db"
    seen_at_announce: list[bool] = []

    def spy(handshake: Handshake, stream: IO[str]) -> None:
        seen_at_announce.append(db_file.is_file())
        handshake_module.announce(handshake, stream)

    monkeypatch.setattr("aegis_core.__main__.announce", spy)
    run()
    assert seen_at_announce == [True]


def test_an_unusable_database_means_no_server(served: _Served, tmp_path: Path) -> None:
    (tmp_path / "Aegis").mkdir()
    (tmp_path / "Aegis" / "aegis.db").write_bytes(b"not a database " * 300)
    stdout = _Pipe()
    with pytest.raises(StorageError):
        entry.main([], stdin=io.StringIO(f"{TOKEN}\n"), stdout=stdout)
    assert served.port is None
    assert stdout.getvalue() == ""


# --------------------------------------------------------------------------- #
# DPI awareness (P2-01) — first, because the first caller wins
# --------------------------------------------------------------------------- #


def test_dpi_awareness_is_set_before_the_token_is_even_read(
    served: _Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def aware() -> DpiAwareness:
        order.append("dpi")
        return DpiAwareness.PER_MONITOR

    monkeypatch.setattr(entry, "ensure_dpi_awareness", aware)

    def token(stream: IO[str]) -> str:
        order.append("token")
        return handshake_module.read_token(stream)

    monkeypatch.setattr(entry, "read_token", token)
    run()
    assert order == ["dpi", "token"]


def test_the_core_still_starts_when_it_cannot_be_made_dpi_aware(
    served: _Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perception refuses on its own (`query_layout`); the Models screen must still open."""
    monkeypatch.setattr(entry, "ensure_dpi_awareness", lambda: DpiAwareness.UNAWARE)
    _, line = run()
    assert line["port"] == served.port


def test_the_real_core_process_ends_up_per_monitor_aware(served: _Served) -> None:
    run()
    assert current_dpi_awareness() is DpiAwareness.PER_MONITOR


def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        entry.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__
