"""Entry point for the packaged sidecar: `aegis-core.exe --port 0`.

Runs the core's half of the `ARCHITECTURE.md § 3.1` startup handshake, then serves
the FastAPI app on the socket it bound:

0. Make the process Per-Monitor DPI aware before anything else can set it, so every
   screen position the core ever reads is a physical pixel (`perception/display.py`).
1. Read the 256-bit session token from the **stdin pipe** — never from argv.
   Then create or migrate `aegis.db`; a core that cannot use its database does not
   announce itself, so MAIN sees a failed start rather than a later failed task.
2. Bind `127.0.0.1:<port>`; `0` (the default) asks the OS for an ephemeral one.
3. Write `{"port","pid","version"}` as one JSON line on stdout, then close stdout.
4. Serve, rejecting anything that is not MAIN with a bare `401`, and watching the
   supervising process so the core stops within 2 s of MAIN dying (step 6).

Step 4 of § 3.1 — the health check and the 3-strike respawn — is MAIN's side of the
supervision and lives in `main/supervisor.ts`.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Final

import uvicorn
from fastapi import FastAPI

from aegis_core import __version__
from aegis_core.logging_setup import configure_logging
from aegis_core.models.service import ModelService
from aegis_core.parent_watch import ParentWatch
from aegis_core.perception.display import ensure_dpi_awareness
from aegis_core.server.app import create_app
from aegis_core.server.auth import SessionAuth
from aegis_core.server.handshake import (
    Handshake,
    HandshakeError,
    announce,
    bind_loopback,
    read_token,
)
from aegis_core.server.hub import EventHub
from aegis_core.storage.db import StorageError, bootstrap
from aegis_core.storage.settings import SettingsStore
from aegis_core.storage.usage import UsageLedger
from aegis_core.storage.vault import KeyVault, VaultError

log = logging.getLogger(__name__)

#: Exit code when the core stopped because its supervisor did.
EXIT_SUPERVISOR_LOST: Final = 3

#: Exit code when `aegis.db` could not be opened or migrated.
EXIT_STORAGE_FAILED: Final = 4

#: How long uvicorn gets to unwind before the process ends outright. Together with
#: the 0.5 s poll interval this keeps the core inside § 3.1 step 6's 2 s budget.
SHUTDOWN_GRACE_S: Final = 1.0


def _model_service(hub: EventHub) -> ModelService | None:
    """The Models screen's service, or `None` if this machine cannot give it one.

    `bootstrap()` has already proved the database, so the one thing that can fail here is
    the vault: `KeyVault` refuses to exist without Windows Credential Manager rather than
    quietly storing keys somewhere else (invariant 9, `P1-07`). That is a machine a user
    cannot save a key on, which is a Models screen that says so — not a core that refuses
    to start, since everything else still works.
    """
    try:
        vault = KeyVault()
    except VaultError as error:
        log.error("core.vault_unavailable", extra={"error": str(error)})
        return None
    return ModelService(SettingsStore.open(), vault, UsageLedger.open(), publisher=hub)


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


def _hard_exit() -> None:
    """End the process now, flushing the logs first.

    Reached only when uvicorn did not stop on its own — a wedged request handler,
    a stuck shutdown. The core has mouse control and its UI is already gone, so
    `os._exit` is the correct tool: no atexit hook, no thread, and no in-flight
    request gets to extend its life.
    """
    log.error("core.exit_forced", extra={"reason": "supervisor_lost"})
    logging.shutdown()
    os._exit(EXIT_SUPERVISOR_LOST)


def _stop_server(server: uvicorn.Server) -> None:
    """Ask uvicorn to stop, and make sure it does."""
    server.should_exit = True
    timer = threading.Timer(SHUTDOWN_GRACE_S, _hard_exit)
    timer.daemon = True
    timer.start()


def _serve(app: FastAPI, sock: socket.socket, *, supervisor_pid: int) -> None:
    """Serve the already-bound socket until the server or the supervisor stops.

    uvicorn is given the socket rather than a port because the ephemeral port has
    to be announced *before* the server starts. `log_config=None` keeps uvicorn's
    own lines flowing through our handlers — its default config would put a second,
    plain-text copy on stdout, which § 3.1 reserves for the handshake line.

    The parent watch is what makes § 3.1 step 6 true when MAIN dies without being
    able to clean up after itself.
    """
    server = uvicorn.Server(uvicorn.Config(app, log_config=None))
    watch = ParentWatch(supervisor_pid, lambda: _stop_server(server))
    watch.start()
    try:
        server.run(sockets=[sock])
    finally:
        watch.stop()


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

    # First, because the process default can be set only once and the first caller wins.
    # A core that could not be made aware still starts: `query_layout()` refuses to hand
    # out coordinates under it, and the failure is already in the log.
    ensure_dpi_awareness()

    models: ModelService | None = None
    token = read_token(stdin if stdin is not None else sys.stdin)
    bootstrap()
    sock = bind_loopback(args.port)
    try:
        port = sock.getsockname()[1]
        supervisor_pid = args.supervisor_pid if args.supervisor_pid is not None else os.getppid()
        auth = SessionAuth(token=token, supervisor_pid=supervisor_pid, local_port=port)
        hub = EventHub()
        models = _model_service(hub)
        app = create_app(auth, hub, models)

        announce(
            Handshake(port=port, pid=os.getpid(), version=__version__),
            stdout if stdout is not None else sys.stdout,
        )
        if stdout is None:
            # stdout is closed now. Point the name at a sink so that a stray
            # write somewhere in a dependency cannot take the process down.
            sys.stdout = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
        log.info("core.listening", extra={"port": port, "supervisor_pid": supervisor_pid})

        _serve(app, sock, supervisor_pid=supervisor_pid)
    finally:
        sock.close()
        if models is not None:
            # The app's lifespan releases the provider pools; these are the two database
            # connections, and they have to go even on the paths where the server never
            # ran and the lifespan therefore never did.
            models.close()


if __name__ == "__main__":
    try:
        main()
    except HandshakeError as error:
        # No token, no port, no core. Stderr carries it; stdout stays clean so a
        # MAIN that is still reading sees an EOF rather than a half-line.
        log.error("core.handshake_failed", extra={"error": str(error)})
        sys.stderr.write(f"aegis-core: {error}\n")
        raise SystemExit(2) from error
    except StorageError as error:
        log.error("core.storage_failed", extra={"error": str(error)})
        sys.stderr.write(f"aegis-core: {error}\n")
        raise SystemExit(EXIT_STORAGE_FAILED) from error
