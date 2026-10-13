"""Rule-dashboard state added by `P3-21` (`UI.md § 8.3`).

Always-allow rules gain a durable match count. `ask_rules` is deliberately only a
tool name: it can narrow the Guardian's answer to `confirm`, never grant access.
"""

from __future__ import annotations

from typing import Final

SQL: Final = """
ALTER TABLE allow_rules
ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0);

CREATE TABLE ask_rules (
    tool        TEXT PRIMARY KEY CHECK (length(tool) BETWEEN 1 AND 128),
    created_at  TEXT NOT NULL
) STRICT;
"""
