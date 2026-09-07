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

## Checks

```powershell
ruff check .      # lint
ruff format .     # format
mypy .            # strict type check
pytest            # tests
```
