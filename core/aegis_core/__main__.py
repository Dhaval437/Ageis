"""Entry point for the packaged sidecar: `aegis-core.exe --port 0 --token-fd <pipe>`.

Scaffold only. The real startup sequence — bind 127.0.0.1 on an ephemeral port,
read the session token from the stdin pipe, emit one JSON line on stdout, then
close stdout — is ARCHITECTURE.md § 3.1 and lands in P0-05 / P0-06.
"""

from __future__ import annotations


def main() -> None:
    """Start the core. Not implemented yet."""
    raise SystemExit("aegis-core is a scaffold; the server lands in P0-05.")


if __name__ == "__main__":
    main()
