"""The FORBIDDEN list (`P3-09`): one negative test per entry, and per pattern.

`REVIEW.md § 6` asks for "one negative test per FORBIDDEN entry proving it is
denied". Here every entry in the shipped `rules.yaml` has examples in `DENIED`, each
is run through the real `Guardian` and must come back `deny` / `forbidden`, and
`test_every_pattern_is_exercised` fails if any single pattern has no example that
matches it — so an entry added without a test cannot pass, and neither can a
pattern that silently matches nothing (a typo in a folder name, a regex that never
fires).

`ALLOWED` is the other half: ordinary work next to each forbidden place — reading
`notepad.exe`, a browser's history, `vssadmin list`, `Get-Date -Format` — that the
list must not catch. A FORBIDDEN list that blocks the ordinary is one a user learns
to route around.

The examples are built from this machine's own environment, so they test the
paths the rules actually resolve to here.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from aegis_core.guardian import rules
from aegis_core.guardian.policy import Guardian, TaskContext
from aegis_core.guardian.rules import (
    ForbiddenEntry,
    Rules,
    Target,
    aegis_install_dir,
    load_rules,
    parse_rules,
)
from aegis_core.server.schemas import RiskTier
from aegis_core.storage.db import default_data_dir
from pydantic import JsonValue

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths and registry")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"%{name}% is not set on this machine")
    return value


def p(access: str, base: str, *parts: str) -> Target:
    return Target("path", "\\".join([base, *parts]), access)  # type: ignore[arg-type]


def reg(access: str, key: str) -> Target:
    return Target("registry", key, access)  # type: ignore[arg-type]


def cmd(text: str) -> Target:
    return Target("command", text)


def denied() -> dict[str, list[Target]]:
    windir, pf, pf86 = env("WINDIR"), env("PROGRAMFILES"), env("PROGRAMFILES(X86)")
    appdata, local, pdata = env("APPDATA"), env("LOCALAPPDATA"), env("PROGRAMDATA")
    home = env("USERPROFILE")
    chrome = f"{local}\\Google\\Chrome\\User Data\\Default"
    firefox = f"{appdata}\\Mozilla\\Firefox\\Profiles\\abc123.default-release"
    start = "Microsoft\\Windows\\Start Menu\\Programs\\Startup"
    run = "Software\\Microsoft\\Windows\\CurrentVersion"
    nt = "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion"
    return {
        "windows-system-files": [
            p("write", windir, "System32", "drivers", "etc", "hosts"),
            p("write", windir, "evil.dll"),
        ],
        "installed-programs": [
            p("write", pf, "Vendor", "app.exe"),
            p("write", pf86, "Vendor", "app.dll"),
        ],
        "startup-folders": [
            p("write", appdata, start, "run-me.lnk"),
            p("write", pdata, start, "run-me.bat"),
        ],
        "windows-credential-stores": [
            p("read", windir, "System32", "config", "SAM"),
            p("read", appdata, "Microsoft", "Credentials", "ABCDEF"),
            p("read", local, "Microsoft", "Credentials", "ABCDEF"),
            p("read", appdata, "Microsoft", "Protect", "S-1-5-21", "key"),
            p("read", local, "Microsoft", "Vault", "4BF4C442"),
            p("read", pdata, "Microsoft", "Vault", "AC658CB4"),
        ],
        "browser-passwords-and-cookies": [
            p("read", chrome, "Login Data"),
            p("read", chrome, "Login Data-journal"),
            p("read", chrome, "Network", "Cookies"),
            p("read", chrome, "Web Data"),
            p("read", local, "Google", "Chrome", "User Data", "Local State"),
            p("read", local, "Microsoft", "Windows", "INetCookies", "ESE", "container.dat"),
            p("read", appdata, "Opera Software", "Opera Stable", "Login Data"),
            p("read", appdata, "Opera Software", "Opera Stable", "Network", "Cookies"),
            p("read", appdata, "Opera Software", "Opera Stable", "Web Data"),
            p("read", appdata, "Opera Software", "Opera Stable", "Local State"),
            p("read", firefox, "logins.json"),
            p("read", firefox, "key4.db"),
            p("read", firefox, "signons.sqlite"),
            p("read", firefox, "cookies.sqlite-wal"),
        ],
        "ssh-keys": [p("read", home, ".ssh", "id_ed25519"), p("write", home, ".ssh", "config")],
        "gpg-keys": [
            p("read", home, ".gnupg", "private-keys-v1.d", "x.key"),
            p("read", appdata, "gnupg", "pubring.kbx"),
        ],
        "cloud-credentials": [
            p("read", home, ".aws", "credentials"),
            p("read", home, ".azure", "msal_token_cache.json"),
            p("read", appdata, "gcloud", "credentials.db"),
            p("read", home, ".kube", "config"),
        ],
        "token-files": [
            p("read", home, ".git-credentials"),
            p("read", home, ".netrc"),
            p("read", home, "_netrc"),
            p("read", home, ".pypirc"),
        ],
        "password-managers": [
            p("read", home, "Documents", "Passwords.kdbx"),
            p("read", home, "Documents", "Passwords.kdbx.old"),
            p("read", home, "Old.kdb"),
            p("read", local, "1Password", "data", "1password.sqlite"),
            p("read", appdata, "Bitwarden", "data.json"),
            p("read", chrome, "Local Extension Settings", "hdokiejnpimakedhajhdlcegeplioahd"),
            p("read", chrome, "IndexedDB", "chrome-extension_nngceckbapebfimnlniiiahkandclblb_0"),
            p("read", chrome, "Local Extension Settings", "aeblfdkhhhdcdjpifhhbdiojplfjncoa"),
        ],
        "crypto-wallets": [
            p("read", home, "Backups", "wallet.dat"),
            p("read", appdata, "Bitcoin", "wallets", "main", "wallet.dat"),
            p("read", appdata, "Electrum", "wallets", "default_wallet"),
            p("read", appdata, "Exodus", "exodus.wallet", "seed.seco"),
            p("read", appdata, "Ethereum", "keystore", "UTC--2026"),
            p("read", chrome, "Local Extension Settings", "nkbihfbeogaeaoehlefnkodbefgpgknn"),
        ],
        "aegis-data": [
            p("read", str(default_data_dir()), "aegis.db"),
            p("write", str(default_data_dir()), "hotkeys.json"),
        ],
        "aegis-install": [
            p("write", str(aegis_install_dir()), "core", "aegis_core", "guardian", "rules.yaml")
        ],
        "autostart-registry": [
            reg("write", f"HKLM\\{run}\\Run"),
            reg("write", f"HKCU\\{run}\\RunOnce"),
            reg("write", "HKLM\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run"),
            reg("write", f"HKLM\\{run}\\Policies\\Explorer\\Run"),
            reg("write", f"HKCU\\{run}\\Policies\\Explorer\\Run"),
            reg("write", f"{nt}\\Winlogon"),
            reg("write", "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"),
            reg("write", f"{nt}\\Image File Execution Options\\sethc.exe"),
        ],
        "security-settings-registry": [
            reg("write", "HKLM\\Software\\Microsoft\\Windows Defender\\Exclusions\\Paths"),
            reg("write", "HKLM\\Software\\Policies\\Microsoft\\Windows Defender"),
            reg("write", "HKLM\\Software\\Policies\\Microsoft\\WindowsFirewall\\DomainProfile"),
            reg("write", f"HKLM\\{run}\\Policies\\System"),
            reg("write", "HKLM\\System\\CurrentControlSet\\Control\\Lsa"),
        ],
        "services-and-drivers-registry": [
            reg("write", "HKLM\\System\\CurrentControlSet\\Services\\EvilDriver"),
            reg("write", "HKLM\\System\\ControlSet001\\Services\\WinDefend"),
        ],
        "account-database-registry": [
            reg("read", "HKLM\\SAM\\SAM\\Domains"),
            reg("read", "HKEY_LOCAL_MACHINE\\SECURITY\\Policy\\Secrets"),
        ],
        "disk-wiping": [
            cmd("format D: /q /y"),
            cmd("Format-Volume -DriveLetter D"),
            cmd("Clear-Disk -Number 1 -RemoveData"),
            cmd("diskpart /s wipe.txt"),
        ],
        "boot-configuration": [cmd("bcdedit /set {default} safeboot minimal")],
        "backup-deletion": [
            cmd("vssadmin delete shadows /all /quiet"),
            cmd("wmic shadowcopy delete"),
            cmd("Get-WmiObject Win32_ShadowCopy | ForEach-Object { $_.Delete() }"),
            cmd("wbadmin delete catalog -quiet"),
        ],
        "defender-tampering": [
            cmd("Set-MpPreference -DisableRealtimeMonitoring $true"),
            cmd('"%ProgramFiles%\\Windows Defender\\MpCmdRun.exe" -RemoveDefinitions -All'),
            cmd("sc stop WinDefend"),
            cmd("Stop-Service -Name WinDefend -Force"),
        ],
        "firewall-tampering": [
            cmd("netsh advfirewall set allprofiles state off"),
            cmd("Set-NetFirewallProfile -Enabled False"),
        ],
        "uac-tampering": [
            cmd("reg add HKLM\\...\\System /v EnableLUA /t REG_DWORD /d 0 /f"),
        ],
        "services-and-drivers": [
            cmd("sc create evil binPath= C:\\evil.exe type= kernel"),
            cmd("New-Service -Name evil -BinaryPathName C:\\evil.exe"),
            cmd("pnputil /add-driver evil.inf /install"),
            cmd("dism /online /add-driver /driver:evil.inf"),
            cmd("installutil evil.dll"),
        ],
        "log-clearing": [
            cmd("wevtutil cl Security"),
            cmd("Clear-EventLog -LogName Security"),
            cmd("auditpol /clear /y"),
        ],
        "credential-theft": [
            cmd("cmdkey /list"),
            cmd("Get-StoredCredential -Target github"),
            cmd("mimikatz.exe sekurlsa::logonpasswords"),
            cmd("reg save HKLM\\SAM sam.hiv"),
            cmd("rundll32 comsvcs.dll MiniDump 624 lsass.dmp full"),
            cmd("procdump -ma lsass.exe out.dmp"),
            cmd("Get-Process lsass | Out-Minidump -DumpFilePath C:\t"),
        ],
        "autostart-commands": [
            cmd("reg add HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run /v x /d evil.exe"),
            cmd(
                "New-ItemProperty -Path 'HKLM:\\Software\\Microsoft\\Windows NT"
                "\\CurrentVersion\\Winlogon' -Name Shell -Value evil"
            ),
        ],
        "hidden-commands": [
            cmd("powershell -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAKQA="),
            cmd("pwsh.exe -NoProfile -EncodedCommand SQBFAFgA"),
            cmd("powershell /e SQBFAFgA"),
        ],
    }


def allowed() -> list[Target]:
    windir, pf = env("WINDIR"), env("PROGRAMFILES")
    appdata, local, home = env("APPDATA"), env("LOCALAPPDATA"), env("USERPROFILE")
    chrome = f"{local}\\Google\\Chrome\\User Data\\Default"
    run = "Software\\Microsoft\\Windows\\CurrentVersion"
    return [
        p("read", windir, "System32", "notepad.exe"),
        p("read", pf, "Vendor", "app.exe"),
        p("write", home, "Documents", "report.docx"),
        p("write", home, "Desktop", "notes.txt"),
        p("read", chrome, "History"),
        p("read", chrome, "Bookmarks"),
        p("write", appdata, "Microsoft", "Windows", "Start Menu", "Programs", "MyApp.lnk"),
        p("write", local, "Programs", "OtherApp", "settings.json"),
        p("write", local, "Temp", "scratch.txt"),
        p("read", home, ".sshrc-notes.txt"),
        p("read", home, "Documents", "wallet-design.docx"),
        reg("read", f"HKLM\\{run}\\Run"),
        reg("write", "HKCU\\Software\\MyApp\\Settings"),
        reg("write", f"HKCU\\{run}\\Explorer\\Advanced"),
        reg("read", "HKLM\\System\\CurrentControlSet\\Services\\WinDefend"),
        cmd("Get-Date -Format yyyy-MM-dd"),
        cmd("git status"),
        cmd("dotnet format ./src"),
        cmd("echo reformat the document"),
        cmd("vssadmin list shadows"),
        cmd("Get-NetFirewallRule -DisplayName x"),
        cmd("netsh advfirewall show allprofiles"),
        cmd("sc query WinDefend"),
        cmd("Get-Service WinDefend"),
        cmd("Get-MpComputerStatus"),
        cmd("powershell -ExecutionPolicy Bypass -File build.ps1"),
        cmd("powershell -NoProfile -Command Get-ChildItem"),
        cmd("reg query HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"),
        cmd("Get-EventLog -LogName Application -Newest 5"),
        cmd("python -c print(1)"),
    ]


@dataclass(frozen=True)
class Touching:
    """A tool that touches exactly one target."""

    target: Target
    name: str = "test.tool"
    risk: RiskTier = "SAFE"

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        return (self.target,)


@pytest.fixture(scope="module")
def shipped() -> Rules:
    return load_rules()


@pytest.fixture(scope="module")
def guardian(shipped: Rules) -> Guardian:
    return Guardian(shipped)


def only(entry: ForbiddenEntry, pattern: str) -> Rules:
    """The shipped rules reduced to one pattern of one entry."""
    return rules.Rules(
        (entry,), (rules._compile(entry.model_copy(update={"patterns": (pattern,)})),)
    )


# --------------------------------------------------------------------------- #
# Coverage: every entry and every pattern has a test
# --------------------------------------------------------------------------- #


def test_every_entry_has_examples(shipped: Rules) -> None:
    assert sorted(e.id for e in shipped.forbidden) == sorted(denied())


def test_every_pattern_is_exercised(shipped: Rules) -> None:
    examples = denied()
    unexercised = [
        f"{entry.id}: {pattern}"
        for entry in shipped.forbidden
        for pattern in entry.patterns
        if not any(only(entry, pattern).forbidden_match(t) for t in examples[entry.id])
    ]
    assert unexercised == []


# --------------------------------------------------------------------------- #
# One negative test per entry: denied by the real Guardian
# --------------------------------------------------------------------------- #


def _cases() -> list[tuple[str, Target]]:
    # Collected lazily: `env()` may skip, which must not happen at import time.
    if sys.platform != "win32":
        return []
    return [(entry, target) for entry, targets in denied().items() for target in targets]


@pytest.mark.parametrize(
    ("entry", "target"), _cases(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_each_forbidden_example_is_denied(
    guardian: Guardian, shipped: Rules, entry: str, target: Target
) -> None:
    # The most permissive context there is: `trusted`, and a scope holding everything.
    ctx = TaskContext("trusted", in_scope=lambda _path: True)
    verdict = guardian.evaluate(Touching(target), {}, ctx)
    assert (verdict.decision, verdict.stage) == ("deny", "forbidden")
    own = next(e for e in shipped.forbidden if e.id == entry)
    assert Rules((own,), (rules._compile(own),)).forbidden_match(target) is not None
    assert verdict.reason == next(e.reason for e in shipped.forbidden if e.id == verdict.rule_id)


@pytest.mark.parametrize(
    "text",
    [
        "for^mat D: /q",
        "fo`rmat d:",
        '"vss"admin delete shadows /all',
        "vss^admin   delete   shadows",
        "Set-Mp`Preference -DisableRealtimeMonitoring 1",
        "c^m^d^k^e^y /list",
    ],
)
def test_shell_escapes_do_not_hide_a_command(guardian: Guardian, text: str) -> None:
    verdict = guardian.evaluate(Touching(cmd(text)), {}, TaskContext("trusted"))
    assert verdict.stage == "forbidden", text


# --------------------------------------------------------------------------- #
# Ordinary work next to the forbidden places is not caught
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", allowed() if sys.platform == "win32" else [], ids=str)
def test_ordinary_work_is_not_forbidden(shipped: Rules, target: Target) -> None:
    assert shipped.forbidden_match(target) is None, target


# --------------------------------------------------------------------------- #
# Compiled in, and out of the UI's reach
# --------------------------------------------------------------------------- #


def test_the_aegis_variables_cannot_be_moved_by_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AEGIS_DATA", str(tmp_path))
    monkeypatch.setenv("AEGIS_INSTALL", str(tmp_path))
    entry = "  - id: x\n    kind: path\n    patterns: ['%AEGIS_DATA%', '%AEGIS_INSTALL%']\n"
    r = parse_rules("version: 1\nforbidden:\n" + entry + "    reason: No.\n")
    assert r.forbidden_match(Target("path", str(default_data_dir() / "aegis.db"), "read"))
    assert r.forbidden_match(Target("path", str(aegis_install_dir() / "x"), "write"))
    assert not r.forbidden_match(Target("path", str(tmp_path / "x"), "write"))


def test_the_install_directory_of_a_packaged_core(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = "C:\\Users\\someone\\AppData\\Local\\Programs\\Aegis\\resources\\core\\aegis-core.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    assert aegis_install_dir() == Path("C:\\Users\\someone\\AppData\\Local\\Programs\\Aegis")


def test_the_install_directory_in_development_is_the_repository() -> None:
    assert (aegis_install_dir() / "core" / "aegis_core" / "guardian" / "rules.yaml").is_file()


def test_no_route_serves_or_edits_the_rules() -> None:
    from aegis_core.server.app import create_app
    from aegis_core.server.auth import SessionAuth

    app = create_app(auth=SessionAuth(token="t" * 64, supervisor_pid=os.getpid()))
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths, "no routes found"
    assert not [
        path for path in paths if any(w in path.lower() for w in ("rule", "forbid", "guardian"))
    ]


def test_nothing_outside_the_guardian_imports_the_rules_loader() -> None:
    package = Path(rules.__file__).resolve().parents[1]
    offenders = []
    for source in package.rglob("*.py"):
        if source.parent.name == "guardian":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "aegis_core.guardian.rules":
                offenders.append(str(source.relative_to(package)))
    assert offenders == []
