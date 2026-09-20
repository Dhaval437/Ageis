"""The `usage` ledger the budget guard counts (`P1-09`).

`ARCHITECTURE.md § 7`'s eight tables have nowhere to put what one model call cost.
`tasks.cost_cents` is a per-task total, written once the task is over, and there is no
task row at all for a call made outside one (a `P1-10` *Test*). Neither can answer the
two questions a ceiling asks — *what has this task spent* and *what has today spent* —
and a day total that lives in memory is not a ceiling, because a restart clears it.

Three columns carry the decisions:

- **`day`** is the **local** date, not the UTC one. A per-day ceiling is a promise to a
  person about their day, and theirs ends at their midnight.
- **`cost_cents` is nullable**, and `NULL` means *the price is unknown* — an
  OpenRouter model this build has no rate for, a `custom` gateway. It is never written
  as 0. A local model is 0.0, which is the other statement (`P1-06`).
- **`task_id` is nullable**, for a call that belongs to no task.
"""

from __future__ import annotations

from typing import Final

SQL: Final = """
CREATE TABLE usage (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER REFERENCES tasks (id) ON DELETE CASCADE,
    ts              TEXT    NOT NULL,
    day             TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('planner', 'grounder', 'utility')),
    provider_id     TEXT    NOT NULL CHECK (provider_id IN (
                        'openai', 'anthropic', 'google', 'nvidia',
                        'openrouter', 'ollama', 'custom')),
    model           TEXT    NOT NULL,
    input_tokens    INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens   INTEGER NOT NULL CHECK (output_tokens >= 0),
    -- NULL is "the price is unknown", never "free". See the module docstring.
    cost_cents      REAL    CHECK (cost_cents IS NULL OR cost_cents >= 0)
) STRICT;

-- The two totals a ceiling is checked against, before every call.
CREATE INDEX usage_by_day ON usage (day);
CREATE INDEX usage_by_task ON usage (task_id);
"""
