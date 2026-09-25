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
import ipaddress
import ntpath
import os
import re
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from aegis_core.guardian import win32
from aegis_core.storage.db import default_data_dir

#: SHA-256 of `rules.yaml` with CRLF read as LF, so a Windows checkout's line endings
#: do not change it. Update it in the same commit as the file; `test_rules.py` prints
#: the digest the file actually has.
RULES_SHA256: Final = "76f02cf5b6be6d7bbb4ba7ac89f411de0c9ce423beec9179d6be61dda26bcadd"

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

#: Variables the loader computes from the running core rather than the environment
#: (`P3-09`): where Aegis keeps its own data, and where it is installed. The install
#: directory is the user's choice at setup, so it cannot be written into the file.
COMPUTED_VARIABLES: Final = frozenset({"AEGIS_DATA", "AEGIS_INSTALL"})


def aegis_install_dir() -> Path:
    r"""The folder Aegis is installed in, from where this process is running.

    Packaged, the core is `<install>\resources\core\aegis-core.exe`, so the install
    directory is three levels up from the executable. In development it is the
    repository root, which holds `core\aegis_core\` — the agent may not rewrite its
    own source either. Read from the process, never from the environment, so a
    variable cannot move it.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parents[2]
    return Path(__file__).resolve().parents[3]


def _computed(name: str) -> str:
    if name == "AEGIS_DATA":
        return str(default_data_dir())
    return str(aegis_install_dir())


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


#: How many links `_reaches_network()` follows before calling a chain unresolvable.
#: Windows itself gives up at 63; a real chain is one or two.
MAX_LINK_HOPS: Final = 32


def _is_device(path: str) -> bool:
    r"""`\\.\…` (devices, pipes, `\\.\C:` raw volumes) or a leftover `\\?\…` namespace."""
    return path.startswith(("\\\\.\\", "\\\\?\\"))


def _is_local_host(host: str) -> bool:
    """Whether a UNC host names this machine, by any spelling that needs no DNS."""
    name = host.strip("[]").rstrip(".").lower()
    if name in {"", ".", "?", "??", "localhost"} or name.endswith(
        (".localhost", ".ipv6-literal.net")
    ):
        return True
    own = {os.environ.get("COMPUTERNAME", "").lower(), socket.gethostname().lower()}
    if name in own - {""}:
        return True
    try:
        # `inet_aton` reads every legacy spelling Windows does: 127.1, 0x7f.1, 2130706433.
        first = socket.inet_aton(name)[0]
        return first in (0, 127)
    except OSError:
        pass
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _usable_share(path: str) -> bool:
    r"""A `\\host\share\…` path the Guardian can judge by its spelling alone.

    Not an admin share (`C$`, `ADMIN$`) and not this machine by another name
    (`\\localhost\C$\Windows` *is* `C:\Windows`): either would reach a local place
    under a spelling no rule is written against. A share on another host is fine —
    FORBIDDEN places are local, and scope decides the rest.
    """
    parts = path[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False
    host, share = parts[0], parts[1]
    return not share.endswith("$") and not _is_local_host(host.split("@", 1)[0])


def _is_remote_drive(path: str) -> bool:
    drive = ntpath.splitdrive(path)[0]
    return len(drive) == 2 and win32.drive_type(drive + "\\") == win32.DRIVE_REMOTE


def _reaches_network(path: str, hops: int = 0) -> bool:
    r"""Whether resolving `path` would pass through a link to a network location.

    `realpath` follows symlinks by opening them, so a symlink to `\\host\share` makes
    it open an SMB session — measured here: 42 s to time out against an unroutable
    host, and then it returned the *unresolved* path. So each existing component is
    `lstat`ed (which does not follow) and each link's target read with `readlink`
    (which reads the reparse data, not the target), and a chain that reaches a UNC
    path, a mapped network drive or a device — or cannot be read, or is too long — is
    reported, so the caller refuses it before `realpath` ever runs.
    """
    if hops > MAX_LINK_HOPS or _is_remote_drive(path):
        return True
    drive, rest = ntpath.splitdrive(path)
    parts = [part for part in rest.split("\\") if part]
    current = drive + "\\"
    for index, part in enumerate(parts):
        current = ntpath.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return False  # nothing exists from here on, so there is nothing to follow
        is_reparse = getattr(info, "st_file_attributes", 0) & win32.FILE_ATTRIBUTE_REPARSE_POINT
        is_link = getattr(info, "st_reparse_tag", 0) & win32.REPARSE_TAG_NAME_SURROGATE
        if not (is_reparse and is_link):
            continue
        try:
            target = str(Path(current).readlink())
        except (OSError, ValueError):
            return True
        if target.lower().startswith("\\\\?\\volume{"):
            return False  # a volume mounted in a folder: local, and `realpath` resolves it
        target = _strip_verbatim(target)
        if target.startswith("\\\\"):
            return True
        if not ntpath.isabs(target):
            target = ntpath.join(ntpath.dirname(current), target)
        return _reaches_network(ntpath.abspath(ntpath.join(target, *parts[index + 1 :])), hops + 1)
    return False


def canonical_path(value: str) -> str | None:
    r"""The one spelling of a path that matching compares, or `None` if it has none.

    `None` for anything that cannot be judged without guessing — a guess is how a
    FORBIDDEN place gets reached by a different name: an empty, relative, over-long or
    NUL-containing path; the device namespace (`\\.\C:\…`, `\\.\PhysicalDrive0`, a
    pipe, a DOS device such as `NUL`); an admin share or this machine under another
    name (`\\localhost\C$\…`); and a local path that reaches the network through a link.

    Otherwise: verbatim prefix removed, `/` read as `\`, `..` resolved, symlinks,
    junctions, `subst` drives and 8.3 names followed as far as the path exists, each
    component read the way Win32 reads it, an alternate data stream (`file:stream`)
    reduced to its file, and the case folded. A path on another machine — UNC or a
    mapped network drive — is canonicalised by its spelling and **never opened**:
    resolving it would open an SMB session to a host the model named, which costs a
    timeout and can hand that host the user's NTLM hash.
    """
    if not value or "\x00" in value or len(value) > MAX_PATH_CHARS:
        return None
    path = _strip_verbatim(value.replace("/", "\\"))
    if not ntpath.isabs(path) or (ntpath.splitdrive(path)[0] == "" and not path.startswith("\\\\")):
        return None
    try:
        path = ntpath.abspath(path)
        if _is_device(path):
            # `\\.\C:\…`, a pipe, and `C:\x\NUL`, which `abspath` turns into `\\.\nul`.
            return None
        if path.startswith("\\\\"):
            if not _usable_share(path):
                return None
        elif not _is_remote_drive(path):
            if _reaches_network(path):
                return None
            path = _strip_verbatim(os.path.realpath(path))
            if path.startswith("\\\\"):
                return None  # a link resolved off the machine after all
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
        if name in COMPUTED_VARIABLES:
            return _computed(name)
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


#: What shells strip before they run a word: cmd's `^` escape, PowerShell's backtick
#: escape, and the quotes that split a word into pieces (`for^mat`, `fo`rmat`,
#: `"vss"admin`). Removing them can only make a command *more* like a rule.
_SHELL_NOISE: Final = str.maketrans("", "", "^`\"'")
_WHITESPACE: Final = re.compile(r"\s+")


def command_forms(text: str) -> tuple[str, ...]:
    """The command as written, and with shell escapes and quotes removed.

    A rule is searched in both, and either matching is a match: the plain form is
    what the rule's author wrote against, the other is what the shell will run.
    This is not a parser. It defeats the cheapest disguises, and the shell tool is
    DANGEROUS anyway, so a person approves whatever gets past it.
    """
    stripped = _WHITESPACE.sub(" ", text.translate(_SHELL_NOISE))
    return (text,) if stripped == text else (text, stripped)


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
            return any(
                regex.search(form) for form in command_forms(canonical) for regex in self.commands
            )
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
