"""Entry point for the packaged sidecar: `aegis-core.exe --port 0`.

Runs the core's half of the `ARCHITECTURE.md § 3.1` startup handshake, then serves
the FastAPI app on the socket it bound:

1. Read the 256-bit session token from the **stdin pipe** — never from argv.
2. Bind `127.0.0.1:<port>`; `0` (the default) asks the OS for an ephemeral one.
3. Write `{"port","pid","version"}` as one JSON line on stdout, then close stdout.
4. Serve, rejecting anything that is not MAIN with a bare `401`.

Steps 4 and 6 of § 3.1 — the health-check/3-strike respawn and the core exiting when
MAIN dies — belong to MAIN's supervisor and are P0-07.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

import uvicorn
from fastapi import FastAPI

from aegis_core import __version__
from aegis_core.logging_setup import configure_logging
from aegis_core.server.app import create_app
from aegis_core.server.auth import SessionAuth
from aegis_core.server.handshake import (
    Handshake,
    HandshakeError,
    announce,
    bind_loopback,
    read_token,
)

log = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aegis-core", description="The AEGIS agent core.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port on 127.0.0.1. 0 picks an ephemeral port (default).",
    )
    parser.add_argument(
        "--supervisor-pid",
        type=int,
        default=None,
        help=(
            "PID of the supervising MAIN process; only it and its descendants may "
            "connect. MAIN always passes this. The fallback is this process' parent, "
            "which is only correct when nothing re-execs between the two."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def _serve(app: FastAPI, sock: socket.socket) -> None:
    """Serve the already-bound socket.

    uvicorn is given the socket rather than a port because the ephemeral port has
    to be announced *before* the server starts. `log_config=None` keeps uvicorn's
    own lines flowing through our handlers — its default config would put a second,
    plain-text copy on stdout, which § 3.1 reserves for the handshake line.
    """
    server = uvicorn.Server(uvicorn.Config(app, log_config=None))
    server.run(sockets=[sock])


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Start the core. Returns when the server stops."""
    args = _parse_args(argv)
    log_file = configure_logging()
    log.info(
        "core.starting",
        extra={"version": __version__, "port": args.port, "log_file": str(log_file)},
    )

    token = read_token(stdin if stdin is not None else sys.stdin)
    sock = bind_loopback(args.port)
    try:
        port = sock.getsockname()[1]
        supervisor_pid = args.supervisor_pid if args.supervisor_pid is not None else os.getppid()
        auth = SessionAuth(token=token, supervisor_pid=supervisor_pid, local_port=port)
        app = create_app(auth)

        announce(
            Handshake(port=port, pid=os.getpid(), version=__version__),
            stdout if stdout is not None else sys.stdout,
        )
        if stdout is None:
            # stdout is closed now. Point the name at a sink so that a stray
            # write somewhere in a dependency cannot take the process down.
            sys.stdout = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
        log.info("core.listening", extra={"port": port, "supervisor_pid": supervisor_pid})

        _serve(app, sock)
    finally:
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except HandshakeError as error:
        # No token, no port, no core. Stderr carries it; stdout stays clean so a
        # MAIN that is still reading sees an EOF rather than a half-line.
        log.error("core.handshake_failed", extra={"error": str(error)})
        sys.stderr.write(f"aegis-core: {error}\n")
        raise SystemExit(2) from error
