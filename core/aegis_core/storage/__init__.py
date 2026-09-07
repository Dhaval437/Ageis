"""SQLite (WAL), migrations, the hash-chained audit log, and the key vault.

Keys live in Windows Credential Manager via DPAPI - never in argv, env vars,
logs, error payloads, or the renderer (invariant 9)."""
