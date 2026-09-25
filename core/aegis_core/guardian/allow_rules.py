"""Always-allow rules (`P3-11`): what *Allow always ▾* creates, and what it matches.

`UI.md § 5` offers three scopes and never a bare "allow everything forever":

- **`exact`** — this tool with exactly these params (compared by a SHA-256 of their
  canonical JSON, so the params are not stored).
- **`tool_in_folder`** — this tool, when every path it touches is inside one folder.
  The folder is the one the approved call's own paths share (`folder_for()`); the
  person picks the *kind*, and never types or supplies a path.
- **`tool_for_task`** — this tool, for the rest of this task only.

A rule matches; whether a match may *relax* anything is `policy.py`'s decision, and it
is narrow on purpose: only a `confirm`, only under `trusted` autonomy (`§ 10`), never
for a call whose effective tier is `DANGEROUS` (invariant 4), and never over the
`confirm` that text echoed from the screen forces (`§ 8.6`). `eligible()` is the same
test, asked when the approval is offered, so the dialog never offers a rule that could
never apply.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue

from aegis_core.guardian.rules import Target, canonical_target
from aegis_core.server.schemas import RuleKind
from aegis_core.storage.allow_rules import StoredRule

if TYPE_CHECKING:
    from aegis_core.guardian.policy import Verdict

#: Longest tool name a rule is kept for; real names are a few words.
MAX_TOOL_CHARS: Final = 128


class RuleError(Exception):
    """A rule that cannot be made. The message is for the user and quotes no path."""


def params_digest(params: Mapping[str, JsonValue]) -> str:
    """SHA-256 of the params as canonical JSON: key order and spacing do not matter."""
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_paths(targets: Sequence[Target]) -> list[str] | None:
    """Every target's canonical path, or `None` if any target is not a checkable path."""
    paths: list[str] = []
    for target in targets:
        if target.kind != "path":
            return None
        canonical = canonical_target(target)
        if canonical is None:
            return None
        paths.append(canonical)
    return paths


def folder_for(targets: Sequence[Target]) -> str | None:
    """The folder a `tool_in_folder` rule for this call would cover, or `None`.

    The deepest folder holding every path the call touches — the parent of each path
    (a path is treated as a file), so "this tool in this folder" never means the
    file's grandparent unless the call itself spans two folders. `None` when the call
    touches no path, or anything that is not a path: a command or a registry key has
    no folder, and a rule that ignored them would cover them.
    """
    paths = _canonical_paths(targets)
    if not paths:
        return None
    folders = [ntpath.dirname(p) for p in paths]
    common = ntpath.commonpath(folders) if len(folders) > 1 else folders[0]
    _drive, rest = ntpath.splitdrive(common)
    if rest in ("", "\\"):
        return None  # never a whole drive or share: that is not "a folder"
    return common


def eligible(verdict: Verdict) -> bool:
    """Whether *Allow always* may be offered for a call with this verdict."""
    return (
        verdict.decision == "confirm"
        and verdict.tier in ("SAFE", "CAUTION")
        and "echoes_observation" not in verdict.signals
    )


@dataclass(frozen=True, slots=True)
class AllowRule:
    id: int
    kind: RuleKind
    tool: str
    folder: str | None = None
    params_digest: str | None = None
    task_id: str | None = None

    def matches(
        self,
        tool: str,
        params: Mapping[str, JsonValue],
        targets: Sequence[Target],
        task_id: str | None,
    ) -> bool:
        if tool != self.tool:
            return False
        if self.kind == "exact":
            return params_digest(params) == self.params_digest
        if self.kind == "tool_for_task":
            return task_id is not None and task_id == self.task_id
        paths = _canonical_paths(targets)
        if not paths or self.folder is None:
            return False
        return all(p == self.folder or p.startswith(self.folder + "\\") for p in paths)

    @classmethod
    def from_stored(cls, stored: StoredRule) -> AllowRule | None:
        """A stored row as a rule, or `None` for a row this build cannot read."""
        if stored.kind == "exact" and stored.params_digest:
            return cls(stored.id, "exact", stored.tool, params_digest=stored.params_digest)
        if stored.kind == "tool_in_folder" and stored.folder:
            return cls(stored.id, "tool_in_folder", stored.tool, folder=stored.folder)
        if stored.kind == "tool_for_task" and stored.task_id:
            return cls(stored.id, "tool_for_task", stored.tool, task_id=stored.task_id)
        return None


def rule_fields(
    kind: RuleKind,
    tool: str,
    params: Mapping[str, JsonValue],
    targets: Sequence[Target],
    task_id: str | None,
) -> dict[str, str]:
    """What a rule of `kind` for this call stores, or `RuleError` if it cannot be made."""
    if not tool or len(tool) > MAX_TOOL_CHARS:
        raise RuleError("This tool cannot have an always-allow rule.")
    if kind == "exact":
        return {"params_digest": params_digest(params)}
    if kind == "tool_for_task":
        if not task_id:
            raise RuleError("This action does not belong to a task, so it cannot be a task rule.")
        return {"task_id": task_id}
    folder = folder_for(targets)
    if folder is None:
        raise RuleError("This action does not work on files in one folder.")
    return {"folder": folder}
