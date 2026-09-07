"""FastAPI app, routes, WebSocket hub, and bearer-token auth.

Binds 127.0.0.1 on an ephemeral port; rejects any request carrying an Origin
header or a bad token, and any peer outside the supervising PID chain
(ARCHITECTURE.md 3.1). Pydantic models in schemas.py are the single source of
truth for the TS types in packages/shared."""
