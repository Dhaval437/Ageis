r"""Adversarial paths for `rules.canonical_path()` (`P3-10`, `REVIEW.md § 5`).

Each case here was a way past the Guardian before P3-10, found by probing the
normaliser on this machine rather than by reading it:

- `\\.\C:\Windows\System32\x.dll` canonicalised to a device-namespace string that
  no FORBIDDEN rule matches — a write into System32 by another name.
- `\\localhost\C$\Windows\…` (and `127.1`, `2130706433`, this PC's own name…) is the
  local disk through an admin share, under a spelling no rule is written against.
- `\\.\PhysicalDrive0`, `\\.\pipe\…`, `\\.\GLOBALROOT\…` and `C:\x\NUL` (which is the
  device `\\.\nul`) were accepted as paths.
- A symlink to a network share made `realpath` open an SMB session: 42 s against an
  unroutable host, and then it returned the *unresolved* local path.

`subst` drives and junctions are resolved for real (they are local), and the tests
build them for real.
"""

from __future__ import annotations

import _winapi
import os
import string
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from aegis_core.guardian import rules, win32
from aegis_core.guardian.rules import Target, canonical_path, parse_rules

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

HOST = os.environ.get("COMPUTERNAME", "localhost")


@pytest.mark.parametrize(
    "value",
    [
        "\\\\.\\C:\\Windows\\System32\\x.dll",
        "\\\\.\\c:\\windows",
        "\\\\.\\PhysicalDrive0",
        "\\\\.\\pipe\\anything",
        "\\\\.\\GLOBALROOT\\Device\\HarddiskVolume3\\Windows\\x",
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\Windows\\x",
        "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\Windows\\x",
        "C:\\somewhere\\NUL",
        "\\\\??\\C:\\Windows\\x",
        "\\\\?\\\\C:\\Windows\\x",
    ],
    ids=[
        "raw-volume",
        "raw-volume-bare",
        "physical-drive",
        "pipe",
        "globalroot-dot",
        "globalroot-verbatim",
        "volume-guid",
        "nul-device",
        "nt-prefix",
        "doubled-verbatim",
    ],
)
def test_the_device_namespace_has_no_canonical_form(value: str) -> None:
    # Whatever `GetFullPathNameW` says is a device is refused. On Windows 11 that is
    # `NUL` in any folder, but no longer `CON`, which opens as an ordinary file there.
    assert canonical_path(value) is None


@pytest.mark.parametrize(
    "value",
    ["\\\\.\\C:\\Windows\\x", "\\\\.\\pipe\\p", "C:\\somewhere\\NUL"],
    ids=["raw-volume", "pipe", "nul-device"],
)
def test_the_device_check_stands_on_its_own(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # `\\.\` would also fail the "this machine" rule (host `.`); switch that off so
    # this proves the device check itself, not the layer behind it.
    monkeypatch.setattr(rules, "_is_local_host", lambda _host: False)
    assert canonical_path(value) is None


def test_a_link_the_walker_missed_is_still_refused_if_it_resolves_off_the_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Belt and braces: if `realpath` ever lands on a share, the answer is still no.
    monkeypatch.setattr(rules, "_reaches_network", lambda _path, _hops=0: False)
    monkeypatch.setattr("os.path.realpath", lambda _path, **_kw: "\\\\?\\UNC\\host\\share\\x")
    assert canonical_path(str(tmp_path / "x")) is None


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "anything.localhost",
        "127.0.0.1",
        "127.1",
        "127.9.9.9",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
        "0.0.0.0",  # noqa: S104 - a hostname under test, not a bind address
        "[::1]",
        "::1",
        "0--1.ipv6-literal.net",
        HOST,
        HOST.lower(),
        ".",
        "?",
    ],
)
def test_this_machine_by_any_name_is_not_a_network_path(host: str) -> None:
    # `\\localhost\Users\…` would be the local disk under a name no rule matches.
    assert canonical_path(f"\\\\{host}\\Users\\someone\\.ssh\\id_rsa") is None


@pytest.mark.parametrize("share", ["C$", "c$", "ADMIN$", "IPC$", "D$"])
def test_an_admin_share_is_never_a_path_on_any_host(share: str) -> None:
    assert canonical_path(f"\\\\fileserver\\{share}\\Windows\\x") is None


@pytest.mark.parametrize(
    "value", ["\\\\?\\UNC\\localhost\\c$\\Windows\\x", "//localhost/c$/Windows/x"]
)
def test_the_same_through_other_spellings(value: str) -> None:
    assert canonical_path(value) is None


def test_a_share_on_another_host_is_kept_by_its_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: str, *args: object, **kwargs: object) -> str:
        raise AssertionError("realpath opened a network path")

    monkeypatch.setattr("os.path.realpath", refuse)
    assert canonical_path("\\\\FileServer\\Team\\Docs\\..\\a.txt") == "\\\\fileserver\\team\\a.txt"
    assert canonical_path("\\\\server@SSL\\DavWWWRoot\\x") == "\\\\server@ssl\\davwwwroot\\x"


def test_a_bare_host_is_not_a_path() -> None:
    assert canonical_path("\\\\fileserver") is None
    assert canonical_path("\\\\fileserver\\") is None


# --------------------------------------------------------------------------- #
# Links that lead off the machine
# --------------------------------------------------------------------------- #


def symlink(link: Path, target: str) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"cannot create symlinks here (winerror {getattr(error, 'winerror', '?')})")


def test_a_symlink_to_a_share_is_refused_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "innocent"
    symlink(link, "\\\\192.0.2.1\\share")  # TEST-NET-1: routes nowhere

    def refuse(path: str, *args: object, **kwargs: object) -> str:
        raise AssertionError("realpath was asked to follow a link to the network")

    monkeypatch.setattr("os.path.realpath", refuse)
    started = time.perf_counter()
    assert canonical_path(str(link / "x.txt")) is None
    assert canonical_path(str(link)) is None
    assert time.perf_counter() - started < 1.0


def test_a_chain_of_links_that_ends_on_a_share_is_refused(tmp_path: Path) -> None:
    net = tmp_path / "net"
    symlink(net, "\\\\192.0.2.1\\share")
    hop = tmp_path / "hop"
    symlink(hop, str(net))
    local = tmp_path / "local"
    local.mkdir()
    _winapi.CreateJunction(str(tmp_path), str(local / "up"))
    assert canonical_path(str(hop / "x.txt")) is None
    assert canonical_path(str(local / "up" / "hop" / "x.txt")) is None


def test_a_relative_symlink_to_a_share_is_refused(tmp_path: Path) -> None:
    net = tmp_path / "net"
    symlink(net, "\\\\192.0.2.1\\share")
    rel = tmp_path / "rel"
    symlink(rel, "net")
    assert canonical_path(str(rel / "x.txt")) is None


def test_an_endless_chain_is_refused(tmp_path: Path) -> None:
    for i in range(rules.MAX_LINK_HOPS + 2):
        symlink(tmp_path / f"l{i}", str(tmp_path / f"l{i + 1}"))
    (tmp_path / f"l{rules.MAX_LINK_HOPS + 2}").mkdir()
    assert canonical_path(str(tmp_path / "l0" / "x.txt")) is None


def test_local_links_are_still_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    symlink(link, str(real))
    assert canonical_path(str(link / "x.txt")) == canonical_path(str(real / "x.txt"))


def test_a_mapped_network_drive_is_never_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        win32, "drive_type", lambda root: win32.DRIVE_REMOTE if root == "Z:\\" else 3
    )

    def refuse(path: str, *args: object, **kwargs: object) -> str:
        raise AssertionError("realpath opened a mapped network drive")

    monkeypatch.setattr("os.path.realpath", refuse)
    monkeypatch.setattr("os.lstat", refuse)
    assert canonical_path("Z:\\Team\\..\\a.txt") == "z:\\a.txt"


def test_the_drive_type_of_a_local_disk() -> None:
    assert win32.drive_type(os.environ["SYSTEMDRIVE"] + "\\") != win32.DRIVE_REMOTE


# --------------------------------------------------------------------------- #
# subst drives are local, and resolved to what they name
# --------------------------------------------------------------------------- #


@pytest.fixture
def subst_drive(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    used = {d for d in string.ascii_uppercase if Path(f"{d}:\\").exists()}
    free = next((d for d in reversed(string.ascii_uppercase) if d not in used), None)
    if free is None:
        pytest.skip("no free drive letter")
    target = tmp_path / "Vault"
    target.mkdir()
    subst = ["subst", f"{free}:", str(target)]
    subprocess.run(subst, check=True, capture_output=True)  # noqa: S603 - fixed argv
    try:
        yield free, target
    finally:
        undo = ["subst", f"{free}:", "/d"]
        subprocess.run(undo, check=False, capture_output=True)  # noqa: S603 - fixed argv


def test_a_subst_drive_is_resolved_to_the_folder_it_names(subst_drive: tuple[str, Path]) -> None:
    letter, target = subst_drive
    r = parse_rules(
        f"version: 1\nforbidden:\n  - id: v\n    kind: path\n"
        f"    patterns: ['{target}']\n    reason: No.\n"
    )
    assert r.forbidden_match(Target("path", f"{letter}:\\new-file.txt")) is not None
    assert canonical_path(f"{letter}:\\a.txt") == canonical_path(str(target / "a.txt"))
