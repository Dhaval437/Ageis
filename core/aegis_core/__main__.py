"""Entry point for the packaged sidecar: `aegis-core.exe --port <n>`.

Configures structured logging, then serves the FastAPI app on loopback.

**Not here yet:** the `ARCHITECTURE.md § 3.1` handshake — reading the session token
from the stdin pipe, writing the `{"port","pid","version"}` line on stdout and closing
it, bearer auth, and the peer-PID check. That is P0-06. Until it lands, run this with
an explicit port; port `0` binds an ephemeral one that nothing can discover yet.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from typing import Final

import uvicorn

from aegis_core import __version__
from aegis_core.logging_setup import configure_logging
from aegis_core.server.app import create_app

#: The core is never reachable from off the machine. Invariant 10.
LOOPBACK: Final = "127.0.0.1"

log = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aegis-core", description="The AEGIS agent core.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port on 127.0.0.1. 0 picks an ephemeral port (default).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Start the core."""
    args = _parse_args(argv)
    log_file = configure_logging()
    log.info(
        "core.starting",
        extra={"version": __version__, "port": args.port, "log_file": str(log_file)},
    )
    uvicorn.run(
        create_app(),
        host=LOOPBACK,
        port=args.port,
        # Our root handlers already write JSON to stderr and the log file; uvicorn's
        # own config would add a second, plain-text copy on stdout — which § 3.1
        # reserves for the handshake line.
        log_config=None,
    )


if __name__ == "__main__":
    main()
