"""AEGIS agent core.

The Python sidecar (`aegis-core.exe`). It owns the agent loop, the tool
registry, the Guardian policy engine, perception, actuation, and local storage.

It never outlives the UI (REMEMBER.md invariant 14) and it is not the home of
the kill switch (invariant 2) — that lives in the Electron MAIN process.
"""

__version__ = "0.0.0"
