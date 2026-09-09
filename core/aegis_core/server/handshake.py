"""The core's half of the startup handshake (`ARCHITECTURE.md § 3.1`).

Three things happen here, in this order, and none of them may be skipped:

* **The session token arrives on stdin** (step 2). Never on argv — `argv` is
  world-readable through WMI, so a token there is a token any process on the
  machine can read.
* **The socket is bound before the server starts** (step 3), because the
  ephemeral port has to be *known* before it can be announced. `uvicorn` is
  handed the already-listening socket rather than a port to pick itself.
* **One JSON line goes out on stdout, and stdout is then closed** (step 3).
  That line is the only thing the core ever writes to stdout — every log line
  goes to stderr and the log file — so MAIN's reader can parse it without
  guessing which line is the handshake.
"""

from __future__ import annotations

import contextlib
import json
import socket
from dataclasses import dataclass
from typing import IO, Final

#: The core is never reachable from off the machine. `REMEMBER.md` invariant 10.
LOOPBACK: Final = "127.0.0.1"

#: A hex-encoded 256-bit token is 64 characters. The cap only bounds the read.
_MAX_TOKEN_CHARS: Final = 512


class HandshakeError(Exception):
    """The handshake could not complete, so the core must not start."""


def read_token(stream: IO[str]) -> str:
    """Read the session token MAIN wrote to the stdin pipe.

    One line, stripped. A core with no token would be an open port with no
    credential, so a missing or oversized token is fatal rather than defaulted.
    """
    try:
        line = stream.readline(_MAX_TOKEN_CHARS + 2)
    except OSError as error:
        raise HandshakeError("could not read the session token from stdin") from error
    token = line.strip()
    if not token:
        raise HandshakeError("no session token on stdin")
    if len(token) > _MAX_TOKEN_CHARS:
        raise HandshakeError("the session token on stdin is too long")
    return token


def bind_loopback(port: int = 0) -> socket.socket:
    """Bind and listen on loopback, returning the socket for uvicorn to serve.

    `port=0` asks the OS for an ephemeral one; read it back with
    `sock.getsockname()[1]`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: on Windows it lets a *different* process steal a
        # bound port, which is the one thing an ephemeral port must not allow.
        sock.bind((LOOPBACK, port))
        sock.listen()
    except OSError as error:
        sock.close()
        raise HandshakeError(f"could not bind {LOOPBACK}:{port}") from error
    return sock


@dataclass(frozen=True)
class Handshake:
    """The single line the core writes to stdout (`ARCHITECTURE.md § 3.1` step 3)."""

    port: int
    pid: int
    version: str

    def to_line(self) -> str:
        """Exactly one line, no trailing whitespace beyond the newline."""
        payload = {"port": self.port, "pid": self.pid, "version": self.version}
        return json.dumps(payload, separators=(",", ":")) + "\n"


def announce(handshake: Handshake, stream: IO[str]) -> None:
    """Write the handshake line, flush it, and close stdout.

    Closing is what gives MAIN an EOF, and it also means a stray `print`
    anywhere in the core can no longer corrupt the channel — the caller
    redirects `sys.stdout` to a sink afterwards so that stray print does not
    crash the process either.
    """
    try:
        stream.write(handshake.to_line())
        stream.flush()
    except OSError as error:
        raise HandshakeError("could not write the handshake line to stdout") from error
    finally:
        # Closing a pipe MAIN has already dropped is not an error worth raising
        # over — the announcement either landed or it did not.
        with contextlib.suppress(OSError):
            stream.close()
