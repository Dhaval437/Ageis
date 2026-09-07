"""Model router and provider adapters.

Three roles - Planner / Grounder / Utility - each independently mapped by the
user to any provider. Five of seven providers are OpenAI-compatible, so they
share one adapter with different base URLs (REMEMBER.md 4)."""
