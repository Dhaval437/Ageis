"""Scopes (`ARCHITECTURE.md § 8.2`): the agent has a workspace, not the whole disk.

A scope is a **name**, a set of **folders** and a set of **apps**. `policy.py` asks
`Scope.contains()` about every path a tool call would touch — writing outside the
scope is refused, reading outside it is asked about — and `Guardian.check_target()`
asks again at the moment of the operation, because the disk can change between the
decision and the act.

What this module guarantees, and where each guarantee comes from:

- **A folder is stored canonical** (`rules.canonical_path()`: realpath, junctions,
  8.3 names, `..`), so containment is a comparison of two canonical strings on a
  whole-component boundary — `C:\\Work` never contains `C:\\Workshop`.
- **A scope is never the whole disk.** A drive root, a network share's root and a
  folder the FORBIDDEN list covers for any access are refused as scope folders; one
  that does not exist or is not a folder is refused too, since there is nothing to
  canonicalise it against.
- **Nesting collapses.** A folder inside another one in the same scope is dropped, so
  the stored list is exactly what the scope allows, and "what this allows" in the UI
  is the list itself.
- **Bounded.** At most `MAX_FOLDERS` folders and `MAX_APPS` apps, and a short name.

Apps are stored as executable names (`excel.exe`), the one identity a window's
process and a launch request both have. Nothing enforces them yet: the `app` and
`window` tools (`P5`) ask `Scope.admits_app()`.

A folder only enters a scope because the user chose it. Who may *create* a scope,
and how a folder the user picked in the OS dialog is told apart from a string the
renderer made up, is `P3-17`'s — this module validates whatever it is given.
"""

from __future__ import annotations

import logging
import ntpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aegis_core.guardian.rules import Rules, Target, canonical_path
from aegis_core.storage.scopes import ScopeRecord

log = logging.getLogger(__name__)

MAX_FOLDERS: Final = 32
MAX_APPS: Final = 32
MAX_NAME_CHARS: Final = 64

_APP: Final = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,60}\.exe$")


class ScopeError(Exception):
    """A scope that cannot be made. The message is for the user and quotes no path."""


@dataclass(frozen=True, slots=True)
class Scope:
    """A validated scope. Build one with `make_scope()`; the folders are canonical."""

    name: str
    folders: tuple[str, ...]
    apps: tuple[str, ...] = ()
    #: The row id, once stored.
    id: int | None = None

    def contains(self, canonical: str) -> bool:
        """Whether a **canonical** path is one of the folders or inside one."""
        return any(canonical == f or canonical.startswith(f + "\\") for f in self.folders)

    def admits_app(self, executable: str) -> bool:
        """Whether an app, by executable name or path, is one of this scope's apps."""
        return ntpath.basename(executable).lower() in self.apps


#: The scope a task gets when it was given none: nothing on disk, no apps.
EMPTY: Final = Scope(name="", folders=())


def _is_root(canonical: str) -> bool:
    r"""A drive root (`c:\`) or a share root (`\\host\share`): the whole of something."""
    # `splitdrive` reads a share (`\\host\share`) as the drive, so both cases are
    # "nothing after the drive".
    return ntpath.splitdrive(canonical)[1] in ("", "\\")


def scope_folder(path: str, rules: Rules) -> str:
    """`path` as a scope folder: canonical, an existing folder, and allowed to be one."""
    canonical = canonical_path(path)
    if canonical is None:
        raise ScopeError("That folder cannot be used: Aegis could not check where it points.")
    if _is_root(canonical):
        raise ScopeError("A whole drive cannot be a scope. Choose a folder on it.")
    if not canonical.startswith("\\\\") and not Path(canonical).is_dir():
        raise ScopeError("That folder does not exist.")
    if rules.forbidden_match(Target("path", canonical, "read")) is not None:
        raise ScopeError("That folder is one Aegis never touches, so it cannot be in a scope.")
    return canonical


def _collapse(folders: Iterable[str]) -> tuple[str, ...]:
    """Distinct folders, with any folder inside another one dropped, in sorted order."""
    kept: list[str] = []
    for folder in sorted(set(folders), key=len):
        if not any(folder == k or folder.startswith(k + "\\") for k in kept):
            kept.append(folder)
    return tuple(sorted(kept))


def _app(name: str) -> str:
    app = ntpath.basename(name.strip()).lower()
    if not _APP.match(app):
        raise ScopeError("An app is named by its program file, such as excel.exe.")
    return app


def load_scope(record: ScopeRecord, rules: Rules) -> Scope:
    """A stored scope, re-validated as it is read — never trusted as stored.

    A folder that no longer passes (deleted, now covered by a newer FORBIDDEN list, a
    row edited by hand) is **dropped**, never kept: a stored scope can only ever come
    back narrower than it was saved. The count is logged; the paths are not.
    """
    folders: list[str] = []
    for folder in record.folders[:MAX_FOLDERS]:
        try:
            folders.append(scope_folder(folder, rules))
        except ScopeError:
            continue
    apps: list[str] = []
    for app in record.apps[:MAX_APPS]:
        try:
            apps.append(_app(app))
        except ScopeError:
            continue
    dropped = len(record.folders) - len(folders) + len(record.apps) - len(apps)
    if dropped:
        log.warning("scope.narrowed", extra={"id": record.id, "dropped": dropped})
    name = record.name.strip()[:MAX_NAME_CHARS] or f"Scope {record.id}"
    return Scope(name, _collapse(folders), tuple(sorted(set(apps))), record.id)


def make_scope(
    name: str,
    folders: Iterable[str],
    rules: Rules,
    apps: Iterable[str] = (),
    scope_id: int | None = None,
) -> Scope:
    """Validate and build a scope. Raises `ScopeError` with a sentence for the user."""
    clean = name.strip()
    if not clean or len(clean) > MAX_NAME_CHARS or any(ord(c) < 0x20 for c in clean):
        raise ScopeError(f"A scope needs a name of 1 to {MAX_NAME_CHARS} characters.")
    folder_list = list(folders)
    app_list = list(apps)
    if len(folder_list) > MAX_FOLDERS:
        raise ScopeError(f"A scope can have at most {MAX_FOLDERS} folders.")
    if len(app_list) > MAX_APPS:
        raise ScopeError(f"A scope can have at most {MAX_APPS} apps.")
    return Scope(
        name=clean,
        folders=_collapse(scope_folder(f, rules) for f in folder_list),
        apps=tuple(sorted({_app(a) for a in app_list})),
        id=scope_id,
    )
