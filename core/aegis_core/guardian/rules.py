"""Loads `rules.yaml` and matches tool-call targets against its FORBIDDEN entries.

Three properties, each a guard against a way a rules file goes wrong quietly:

- **The file is pinned.** `load_rules()` refuses a `rules.yaml` whose SHA-256 is not
  `RULES_SHA256`, so an edited, truncated or missing copy in an install directory is a
  Guardian that will not start rather than one enforcing rules nobody reviewed. That
  is what "compiled in" means here (REMEMBER.md invariant 5): the digest lives in the
  bytecode, and the data stays readable YAML with a diff.
- **The schema is closed.** Unknown keys, a misspelt `kind`, an empty pattern list,
  an unknown `%VARIABLE%` or a regex that does not compile are load errors — a rule
  that silently matches nothing is worse than no rule, because it reads as one.
- **Matching happens on canonical forms.** Paths go through `canonical_path()` —
  absolute, symlinks and junctions resolved, case folded, the Win32 spellings that name
  the same file (`\\\\?\\` prefixes, trailing dots and spaces, an alternate data stream)
  collapsed — and registry keys through `canonical_key()`. A target that cannot be put
  in canonical form is not matched against anything; `policy.py` denies it instead.

`canonical_path()` is the first, small instance of the path normalisation `P3-10`
owns (8.3 names, UNC shares, adversarial inputs). That task extends this function
rather than writing a second one beside it.

`yaml.safe_load` only: the file is data, and nothing in it may construct an object.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from importlib import resources
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

#: SHA-256 of `rules.yaml` with CRLF read as LF, so a Windows checkout's line endings
#: do not change it. Update it in the same commit as the file; `test_rules.py` prints
#: the digest the file actually has.
RULES_SHA256: Final = "f1312a0481e05bfba270e097d68c4034d89da47dab0d9f06706ca4b61e68b76b"

#: What `%NAME%` may stand for in a path pattern. Anything else is a load error.
PATH_VARIABLES: Final = frozenset(
    {
        "WINDIR",
        "SYSTEMROOT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
    }
)

#: Win32's own limit on a path; anything longer is not a path Windows will open.
MAX_PATH_CHARS: Final = 32_767

#: A target longer than this is not matched; `policy.py` denies it as uncheckable.
MAX_TARGET_CHARS: Final = 65_536

_VARIABLE: Final = re.compile(r"%([^%]+)%")

_HIVES: Final[Mapping[str, str]] = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
}

TargetKind = Literal["path", "command", "registry"]
Access = Literal["read", "write"]


class RulesError(Exception):
    """`rules.yaml` could not be loaded. The Guardian must not run without it."""


@dataclass(frozen=True, slots=True)
class Target:
    """One thing a tool call would touch, as the tool declares it (`policy.GuardedTool`).

    `access` is what the call does to it. A tool that cannot say must say `write`.
    """

    kind: TargetKind
    value: str
    access: Access = "write"


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #

_Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]
_Pattern = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
_Reason = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ForbiddenEntry(BaseModel):
    """One FORBIDDEN rule, as written in `rules.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: _Id
    kind: TargetKind
    access: Literal["any", "write"] = "any"
    # YAML has lists, not tuples; strictness stays on for every value inside.
    patterns: tuple[_Pattern, ...] = Field(min_length=1, strict=False)
    reason: _Reason


class RuleFile(BaseModel):
    """The whole of `rules.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[1]
    forbidden: tuple[ForbiddenEntry, ...] = Field(strict=False)


# --------------------------------------------------------------------------- #
# Canonical forms
# --------------------------------------------------------------------------- #


def _strip_verbatim(path: str) -> str:
    r"""`\\?\C:\x` → `C:\x`, `\\?\UNC\srv\share` → `\\srv\share`: one file, one spelling."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _win32_component(part: str) -> str:
    """How Win32 reads one path component: trailing dots and spaces are dropped."""
    stripped = part.rstrip(". ")
    # `.` and `..` were already resolved by `abspath`; an all-dots name is left alone.
    return stripped if stripped else part


def canonical_path(value: str) -> str | None:
    r"""The one spelling of a path that matching compares, or `None` if it has none.

    `None` for an empty, relative, over-long or NUL-containing path: those cannot be
    resolved without guessing, and a guess is how a FORBIDDEN place gets reached by a
    different name. Otherwise: verbatim prefix removed, `/` read as `\`, `..` resolved,
    symlinks and junctions followed as far as the path exists, each component read the
    way Win32 reads it, an alternate data stream (`file:stream`) reduced to its file,
    and the case folded. A UNC or device path (`\\host\share`) is never opened, so a
    link *inside* a share is not followed.
    """
    if not value or "\x00" in value or len(value) > MAX_PATH_CHARS:
        return None
    path = _strip_verbatim(value.replace("/", "\\"))
    if not ntpath.isabs(path) or (ntpath.splitdrive(path)[0] == "" and not path.startswith("\\\\")):
        return None
    try:
        path = ntpath.abspath(path)
        # Never touch a network path: resolving `\\host\share` opens an SMB session to
        # a host the *model* named, which costs a timeout and can hand that host the
        # user's NTLM hash. UNC and device paths are canonicalised lexically only.
        if not path.startswith("\\\\"):
            path = _strip_verbatim(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    drive, rest = ntpath.splitdrive(path)
    parts = [_win32_component(part) for part in rest.split("\\")]
    # A stream belongs to its file: `Login Data:x` is `Login Data`, for matching.
    if parts and ":" in parts[-1]:
        parts[-1] = _win32_component(parts[-1].split(":", 1)[0])
    joined = drive + "\\".join(parts)
    if len(joined) > len(drive) + 1:
        joined = joined.rstrip("\\")
    return ntpath.normcase(joined)


def canonical_key(value: str) -> str | None:
    """A registry key in one spelling: long hive name, `\\` separators, upper case."""
    if not value or "\x00" in value or len(value) > MAX_PATH_CHARS:
        return None
    parts = [part for part in value.replace("/", "\\").split("\\") if part]
    if parts and parts[0].upper() == "COMPUTER":
        parts = parts[1:]
    if not parts:
        return None
    hive = _HIVES.get(parts[0].upper(), parts[0].upper())
    if hive not in _HIVES.values():
        return None
    return "\\".join([hive, *parts[1:]]).upper()


# --------------------------------------------------------------------------- #
# Compiled rules
# --------------------------------------------------------------------------- #


def _expand(pattern: str, entry: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).upper()
        if name not in PATH_VARIABLES:
            raise RulesError(f"Rule {entry!r} uses %{match.group(1)}%, which is not allowed.")
        value = os.environ.get(name)
        if not value:
            raise RulesError(f"Rule {entry!r} needs %{name}%, which is not set.")
        return value

    return _VARIABLE.sub(replace, pattern)


def _canonical_path_pattern(pattern: str, entry: str) -> str:
    """Canonicalise the part before the first wildcard; the rest is folded only."""
    expanded = _expand(pattern, entry)
    cut = expanded.find("*")
    head, tail = (expanded, "") if cut == -1 else (expanded[:cut], expanded[cut:])
    # The head may end mid-name (`C:\Users\a*`); only whole folders are resolved.
    folder, partial = ntpath.split(head) if cut != -1 else (head, "")
    base = canonical_path(folder)
    if base is None:
        raise RulesError(f"Rule {entry!r} has a pattern that is not an absolute path.")
    joined = ntpath.join(base, partial + tail) if (partial or tail) else base
    return ntpath.normcase(joined)


def _canonical_key_pattern(pattern: str, entry: str) -> str:
    key = canonical_key(pattern)
    if key is None:
        raise RulesError(f"Rule {entry!r} has a pattern that is not a registry key.")
    return key


def _place_matches(target: str, pattern: str) -> bool:
    """A place is matched by itself and by everything under it."""
    return fnmatchcase(target, pattern) or fnmatchcase(target, pattern + "\\*")


@dataclass(frozen=True, slots=True)
class _Compiled:
    entry: ForbiddenEntry
    places: tuple[str, ...]
    commands: tuple[re.Pattern[str], ...]

    def matches(self, target: Target, canonical: str) -> bool:
        if target.kind != self.entry.kind:
            return False
        if self.entry.access == "write" and target.access != "write":
            return False
        if self.entry.kind == "command":
            return any(regex.search(canonical) for regex in self.commands)
        return any(_place_matches(canonical, place) for place in self.places)


def _compile(entry: ForbiddenEntry) -> _Compiled:
    if entry.kind == "command":
        try:
            regexes = tuple(re.compile(p, re.IGNORECASE) for p in entry.patterns)
        except re.error as error:
            raise RulesError(f"Rule {entry.id!r} has a pattern that is not a regex.") from error
        return _Compiled(entry, (), regexes)
    make = _canonical_path_pattern if entry.kind == "path" else _canonical_key_pattern
    return _Compiled(entry, tuple(make(p, entry.id) for p in entry.patterns), ())


def canonical_target(target: Target) -> str | None:
    """`target.value` in the form its kind is matched in, or `None` if it has none."""
    if len(target.value) > MAX_TARGET_CHARS:
        return None
    if target.kind == "path":
        return canonical_path(target.value)
    if target.kind == "registry":
        return canonical_key(target.value)
    return target.value


@dataclass(frozen=True, slots=True)
class Rules:
    """`rules.yaml`, validated and compiled. Build with `load_rules()` or `parse_rules()`."""

    forbidden: tuple[ForbiddenEntry, ...]
    _compiled: tuple[_Compiled, ...]

    def forbidden_match(self, target: Target) -> ForbiddenEntry | None:
        """The first FORBIDDEN entry `target` falls under, or `None`.

        A target with no canonical form matches nothing here; the caller must treat
        that as a refusal in its own right (`canonical_target()`).
        """
        canonical = canonical_target(target)
        if canonical is None:
            return None
        for compiled in self._compiled:
            if compiled.matches(target, canonical):
                return compiled.entry
        return None


def digest(text: str) -> str:
    """The pinned digest of a rules file: SHA-256 of its UTF-8 with CRLF read as LF."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def parse_rules(text: str) -> Rules:
    """Validate and compile a rules document. No digest check: tests and `load_rules()`."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RulesError("The rules file is not valid YAML.") from error
    try:
        parsed = RuleFile.model_validate(document)
    except ValidationError as error:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "(top)" for e in error.errors()})
        raise RulesError(f"The rules file is malformed at: {', '.join(fields)}.") from error
    ids = [entry.id for entry in parsed.forbidden]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RulesError(f"Rule ids must be unique; repeated: {', '.join(duplicates)}.")
    return Rules(parsed.forbidden, tuple(_compile(entry) for entry in parsed.forbidden))


def read_packaged_rules() -> str:
    """The `rules.yaml` shipped beside this module."""
    try:
        return (
            resources.files("aegis_core.guardian")
            .joinpath("rules.yaml")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise RulesError("The Guardian's rules file is missing or unreadable.") from error


def load_rules() -> Rules:
    """The shipped rules, refused unless they are exactly the reviewed ones."""
    text = read_packaged_rules()
    if digest(text) != RULES_SHA256:
        raise RulesError("The Guardian's rules file has been changed since it was reviewed.")
    return parse_rules(text)
