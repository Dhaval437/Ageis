"""Always-allow rules made from an approval's *Allow always* (`P3-11`, `UI.md § 5`).

A rule outlives the task that made it and has to be listed and revoked on the Rules
screen, so it is a table rather than an `approvals` flag. The `CHECK`s make each kind
carry exactly what it matches on and nothing else — an `exact` rule with no digest, or
a folder rule with no folder, would be a rule that silently matches everything.
"""

from __future__ import annotations

from typing import Final

SQL: Final = """
CREATE TABLE allow_rules (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL CHECK (kind IN ('exact', 'tool_in_folder', 'tool_for_task')),
    tool            TEXT    NOT NULL CHECK (length(tool) BETWEEN 1 AND 128),
    folder          TEXT,
    params_digest   TEXT,
    task_id         TEXT,
    created_at      TEXT    NOT NULL,
    -- `IS NOT NULL` spelled out: SQLite passes a CHECK whose value is NULL, and
    -- `length(NULL)` is NULL, so without it a rule with nothing to match on got in.
    CHECK (kind != 'exact' OR (params_digest IS NOT NULL AND length(params_digest) = 64
                               AND folder IS NULL AND task_id IS NULL)),
    CHECK (kind != 'tool_in_folder' OR (folder IS NOT NULL AND length(folder) > 0
                                        AND params_digest IS NULL AND task_id IS NULL)),
    CHECK (kind != 'tool_for_task' OR (task_id IS NOT NULL AND length(task_id) > 0
                                       AND params_digest IS NULL AND folder IS NULL))
) STRICT;
"""
