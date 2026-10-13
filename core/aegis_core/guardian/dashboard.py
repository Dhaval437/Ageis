"""The read-only FORBIDDEN projection shown on the Rules dashboard (`P3-21`).

Only Guardian code loads the digest-pinned rule file. The server receives reviewed
sentences, never the rules object, patterns, paths, regexes, kinds, or identifiers.
"""

from __future__ import annotations

from aegis_core.guardian.rules import load_rules


def forbidden_labels() -> tuple[str, ...]:
    """Reviewed user-facing sentences from the compiled FORBIDDEN entries."""
    return tuple(entry.reason for entry in load_rules().forbidden)
