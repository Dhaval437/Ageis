# aegis-core

The AEGIS Python sidecar (`aegis-core.exe`). Agent loop, tool registry, Guardian
policy engine, perception, actuation, and local storage.

Project docs live in [`../aegis-docs/`](../aegis-docs/) — start with `REMEMBER.md`.

## Local setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Run

```powershell
.\.venv\Scripts\python.exe -m aegis_core --port 8765
curl http://127.0.0.1:8765/v1/health
```

Logs are JSON lines, one object per line, written to `%LOCALAPPDATA%\Aegis\logs\core.log`
and to **stderr**. Nothing is ever written to stdout: it is reserved for the single
handshake line of `ARCHITECTURE.md § 3.1`, which lands with P0-06 — until then the port
must be given explicitly, because nothing yet publishes an ephemeral one.

## Checks

```powershell
ruff check .      # lint
ruff format .     # format
mypy .            # strict type check
pytest            # tests
```
