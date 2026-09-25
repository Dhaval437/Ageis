"""Tests for `guardian/rules.py` — loading `rules.yaml` and matching FORBIDDEN entries.

Paths are this machine's own: a FORBIDDEN place is a folder under `tmp_path`, and the
adversarial spellings of it (junctions, 8.3 names, streams, `\\\\?\\`, trailing dots)
are made for real, because a matcher tested only on strings is a matcher tested only
against the spellings its author thought of.
"""

from __future__ import annotations

import _winapi
import ntpath
import os
import sys
from pathlib import Path

import pytest
from aegis_core.guardian import rules
from aegis_core.guardian.rules import (
    RULES_SHA256,
    Rules,
    RulesError,
    Target,
    canonical_key,
    canonical_path,
    digest,
    load_rules,
    parse_rules,
    read_packaged_rules,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


def one_rule(kind: str, *patterns: str, access: str = "any", rule_id: str = "the-rule") -> Rules:
    quoted = "\n".join(f"      - '{p}'" for p in patterns)
    return parse_rules(
        f"""
version: 1
forbidden:
  - id: {rule_id}
    kind: {kind}
    access: {access}
    patterns:
{quoted}
    reason: Aegis never touches this.
"""
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A FORBIDDEN folder with a file in it, plus a sibling whose name extends it."""
    place = tmp_path / "Vault"
    place.mkdir()
    (place / "secret.db").write_text("x")
    (tmp_path / "VaultPublic").mkdir()
    return place


def junction(link: Path, target: Path) -> None:
    """A real NTFS junction, as `mklink /J` makes; no elevation needed."""
    _winapi.CreateJunction(str(target), str(link))


def matched(r: Rules, value: str, kind: str = "path", access: str = "write") -> bool:
    return r.forbidden_match(Target(kind, value, access)) is not None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The shipped file
# --------------------------------------------------------------------------- #


def test_the_shipped_file_is_the_reviewed_one() -> None:
    actual = digest(read_packaged_rules())
    assert actual == RULES_SHA256, (
        f"rules.yaml changed: if that was deliberate, set RULES_SHA256 = {actual!r}"
    )


def test_the_shipped_file_loads() -> None:
    assert isinstance(load_rules(), Rules)


def test_an_edited_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even a comment: the digest is of the file, not of what it means.
    edited = read_packaged_rules() + "\n# harmless?\n"
    monkeypatch.setattr(rules, "read_packaged_rules", lambda: edited)
    with pytest.raises(RulesError, match="changed since it was reviewed"):
        load_rules()


def test_a_missing_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Gone:
        def joinpath(self, _name: str) -> Gone:
            return self

        def read_text(self, encoding: str) -> str:
            raise FileNotFoundError(encoding)

    monkeypatch.setattr("importlib.resources.files", lambda _package="": Gone())
    with pytest.raises(RulesError, match="missing or unreadable"):
        load_rules()


def test_line_endings_do_not_change_the_digest() -> None:
    text = read_packaged_rules()
    assert digest(text.replace("\n", "\r\n")) == digest(text)


# --------------------------------------------------------------------------- #
# The schema
# --------------------------------------------------------------------------- #


GOOD_ENTRY = """
  - id: a-rule
    kind: command
    patterns: ['format']
    reason: Never.
"""


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("not yaml", "version: [1"),
        ("not a mapping", "- 1\n- 2"),
        # Under an unsafe loader this would construct `int(1)` and load cleanly.
        ("a python tag", "version: !!python/object/apply:builtins.int [1]\nforbidden: []"),
        ("an unknown version", "version: 2\nforbidden: []"),
        ("a quoted version", "version: '1'\nforbidden: []"),
        ("an unknown top-level key", "version: 1\nforbidden: []\nallow: []"),
        ("no forbidden list", "version: 1"),
        ("an unknown key in an entry", "version: 1\nforbidden:" + GOOD_ENTRY + "    extra: 1\n"),
        ("an unknown kind", "version: 1\nforbidden:" + GOOD_ENTRY.replace("command", "file")),
        ("an unknown access", "version: 1\nforbidden:" + GOOD_ENTRY + "    access: read\n"),
        ("no patterns", "version: 1\nforbidden:" + GOOD_ENTRY.replace("['format']", "[]")),
        ("an empty pattern", "version: 1\nforbidden:" + GOOD_ENTRY.replace("'format'", "''")),
        ("an empty reason", "version: 1\nforbidden:" + GOOD_ENTRY.replace("Never.", "''")),
        ("a bad id", "version: 1\nforbidden:" + GOOD_ENTRY.replace("a-rule", "A Rule")),
        ("a numeric pattern", "version: 1\nforbidden:" + GOOD_ENTRY.replace("'format'", "7")),
        ("a duplicate id", "version: 1\nforbidden:" + GOOD_ENTRY + GOOD_ENTRY),
        ("a broken regex", "version: 1\nforbidden:" + GOOD_ENTRY.replace("format", "(unclosed")),
    ],
)
def test_a_malformed_file_is_refused(why: str, text: str) -> None:
    with pytest.raises(RulesError):
        parse_rules(text)


def test_an_error_names_the_field_not_the_value() -> None:
    with pytest.raises(RulesError) as error:
        parse_rules("version: 1\nforbidden:" + GOOD_ENTRY.replace("command", "sEcReT-kind"))
    assert "forbidden.0.kind" in str(error.value)
    assert "sEcReT" not in str(error.value)


def test_a_variable_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(RulesError, match="not allowed"):
        one_rule("path", "%TEMP%")


def test_an_unset_variable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    with pytest.raises(RulesError, match="not set"):
        one_rule("path", "%PROGRAMDATA%")


@pytest.mark.parametrize("pattern", ["relative\\folder", "C:folder", "", "HKLM"])
def test_a_path_pattern_must_be_absolute(pattern: str) -> None:
    with pytest.raises(RulesError):
        one_rule("path", pattern)


@pytest.mark.parametrize("pattern", ["HKEY_NOWHERE\\x", "Software\\Run", "\\\\"])
def test_a_registry_pattern_must_name_a_hive(pattern: str) -> None:
    with pytest.raises(RulesError):
        one_rule("registry", pattern)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def test_a_place_and_everything_under_it_match(vault: Path) -> None:
    r = one_rule("path", str(vault))
    assert matched(r, str(vault))
    assert matched(r, str(vault / "secret.db"))
    assert matched(r, str(vault / "new" / "deeper.txt"))


def test_a_sibling_that_shares_a_prefix_does_not(vault: Path) -> None:
    r = one_rule("path", str(vault))
    assert not matched(r, str(vault.parent / "VaultPublic"))
    assert not matched(r, str(vault.parent / "VaultPublic" / "x.txt"))
    assert not matched(r, str(vault.parent))


@pytest.mark.parametrize(
    "spelling",
    [
        "{v}\\secret.db",
        "{V}\\SECRET.DB",
        "{v}/secret.db",
        "{p}\\VaultPublic\\..\\Vault\\secret.db",
        "{v}\\.\\secret.db",
        "\\\\?\\{v}\\secret.db",
        "{v}\\secret.db:hidden",
        "{v}\\secret.db::$DATA",
        "{v}\\secret.db.",
        "{v}\\secret.db . .",
        "{v}.\\secret.db",
        # Win32 leaves a dot and a space mid-path alone; collapsing it errs towards a match.
        "{v}. \\secret.db",
        "{v}\\\\secret.db",
    ],
)
def test_every_spelling_of_a_forbidden_file_matches(vault: Path, spelling: str) -> None:
    r = one_rule("path", str(vault))
    value = spelling.format(v=vault, V=str(vault).upper(), p=vault.parent)
    assert matched(r, value), value


@pytest.mark.parametrize("exists", [True, False], ids=["existing", "not-yet-created"])
@pytest.mark.parametrize(
    "suffix",
    [":hidden", "::$DATA", ":x:$DATA", ".", " . .", " ", "...", "\\", ".:hidden", " :hidden"],
    ids=[
        "stream",
        "default-stream",
        "typed-stream",
        "dot",
        "dots-spaces",
        "space",
        "dots",
        "sep",
        # Win32 does not strip these two itself: the dot sits before a stream name.
        "dot-then-stream",
        "space-then-stream",
    ],
)
def test_a_rule_naming_a_file_catches_its_other_spellings(
    vault: Path, suffix: str, exists: bool
) -> None:
    # The rule names the file itself, so nothing is matched by being "under" it: the
    # stream and the trailing dots have to be read the way Win32 reads them.
    name = "secret.db" if exists else "Login Data"
    r = one_rule("path", str(vault / name))
    assert matched(r, str(vault / name) + suffix)


def test_a_junction_into_a_forbidden_place_matches(vault: Path, tmp_path: Path) -> None:
    link = tmp_path / "innocent"
    junction(link, vault)
    r = one_rule("path", str(vault))
    assert matched(r, str(link / "secret.db"))
    assert matched(r, str(link / "not-yet-created.txt"))


def test_a_pattern_written_through_a_junction_matches_the_real_place(
    vault: Path, tmp_path: Path
) -> None:
    link = tmp_path / "alias"
    junction(link, vault)
    r = one_rule("path", str(link))
    assert matched(r, str(vault / "secret.db"))


@pytest.mark.skipif(not Path("C:\\PROGRA~1").exists(), reason="no 8.3 name for Program Files")
def test_an_8_3_short_name_matches_the_long_one() -> None:
    r = one_rule("path", "%PROGRAMFILES%")
    assert matched(r, "C:\\PROGRA~1\\SomeApp\\app.exe")
    assert matched(r, "C:\\PROGRA~1\\not-there\\new.txt")


def test_a_wildcard_crosses_folders(tmp_path: Path) -> None:
    r = one_rule("path", str(tmp_path / "Profiles" / "*" / "Login Data"))
    assert matched(r, str(tmp_path / "Profiles" / "Default" / "Login Data"))
    assert matched(r, str(tmp_path / "Profiles" / "a" / "b" / "Login Data"))
    assert not matched(r, str(tmp_path / "Profiles" / "Default" / "History"))
    # A place is its own name, never a longer one: SQLite's `Login Data-journal` needs
    # its own pattern (or `Login Data*`), exactly as `Vault` does not cover `VaultPublic`.
    assert not matched(r, str(tmp_path / "Profiles" / "Default" / "Login Data-journal"))
    r = one_rule("path", str(tmp_path / "Profiles" / "*" / "Login Data*"))
    assert matched(r, str(tmp_path / "Profiles" / "Default" / "Login Data-journal"))


def test_a_wildcard_mid_name(tmp_path: Path) -> None:
    r = one_rule("path", str(tmp_path / "wallet*"))
    assert matched(r, str(tmp_path / "wallet.dat"))
    assert matched(r, str(tmp_path / "Wallet-backup" / "keys"))
    assert not matched(r, str(tmp_path / "my-wallet.dat"))


def test_a_variable_expands(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("APPDATA", str(vault))
    r = one_rule("path", "%APPDATA%")
    assert matched(r, str(vault / "secret.db"))


def test_write_only_rules_leave_reading_alone(vault: Path) -> None:
    r = one_rule("path", str(vault), access="write")
    assert matched(r, str(vault / "secret.db"), access="write")
    assert not matched(r, str(vault / "secret.db"), access="read")


def test_any_access_rules_catch_reading_too(vault: Path) -> None:
    r = one_rule("path", str(vault))
    assert matched(r, str(vault / "secret.db"), access="read")


def test_a_rule_only_matches_its_own_kind(vault: Path) -> None:
    r = one_rule("path", str(vault))
    assert not matched(r, str(vault), kind="command")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "relative\\file.txt",
        "C:file.txt",
        "file.txt",
        "C:\\with\x00nul",
        "C:\\" + "a" * 40_000,
    ],
    ids=["empty", "relative", "drive-relative", "bare", "nul", "over-long"],
)
def test_a_path_with_no_canonical_form_has_none(value: str) -> None:
    assert canonical_path(value) is None


def test_a_uncheckable_target_matches_nothing(vault: Path) -> None:
    # Not a pass: `policy.py` refuses a target with no canonical form.
    r = one_rule("path", str(vault))
    assert not matched(r, "relative\\secret.db")


def test_a_network_path_is_never_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: str, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"realpath was asked to open {path!r}")

    monkeypatch.setattr("os.path.realpath", refuse)
    assert canonical_path("\\\\attacker.example\\share\\x.txt") == ntpath.normcase(
        "\\\\attacker.example\\share\\x.txt"
    )
    assert canonical_path("\\\\?\\UNC\\attacker.example\\share\\x") == ntpath.normcase(
        "\\\\attacker.example\\share\\x"
    )


def test_a_network_place_matches_lexically() -> None:
    r = one_rule("path", "\\\\fileserver\\secrets")
    assert matched(r, "\\\\FILESERVER\\Secrets\\a\\..\\b.txt")
    assert not matched(r, "\\\\fileserver\\secretsauce\\b.txt")


def test_canonical_paths_are_stable(vault: Path) -> None:
    once = canonical_path(str(vault / "secret.db"))
    assert once is not None
    assert canonical_path(once) == once


# --------------------------------------------------------------------------- #
# Registry keys and commands
# --------------------------------------------------------------------------- #

RUN = "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"


@pytest.mark.parametrize(
    "value",
    [
        "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
        "hklm\\software\\microsoft\\windows\\currentversion\\run",
        "HKLM/Software/Microsoft/Windows/CurrentVersion/Run",
        "Computer\\HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
        RUN + "\\",
        RUN + "\\Evil",
        "HKLM\\\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
    ],
)
def test_every_spelling_of_a_forbidden_key_matches(value: str) -> None:
    assert matched(one_rule("registry", RUN), value, kind="registry"), value


@pytest.mark.parametrize(
    "value",
    [
        RUN + "Once",
        "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
        "HKLM\\Software\\Microsoft",
    ],
)
def test_neighbouring_keys_do_not_match(value: str) -> None:
    assert not matched(one_rule("registry", RUN), value, kind="registry")


@pytest.mark.parametrize("value", ["", "Software\\Run", "HKEY_NOWHERE\\x", "Computer"])
def test_a_key_with_no_hive_has_no_canonical_form(value: str) -> None:
    assert canonical_key(value) is None


def test_a_command_rule_searches_case_insensitively() -> None:
    r = one_rule("command", r"\bvssadmin\b.*\bdelete\b")
    assert matched(r, "VSSADMIN Delete Shadows /all", kind="command")
    assert matched(r, "cmd /c vssadmin   delete shadows", kind="command")
    assert not matched(r, "vssadmin list shadows", kind="command")


def test_an_over_long_target_matches_nothing() -> None:
    # `policy.py` refuses it; the matcher does not spend a regex on it.
    r = one_rule("command", "format")
    assert not matched(r, "format " + "x" * rules.MAX_TARGET_CHARS, kind="command")


def test_the_first_matching_rule_is_reported(vault: Path) -> None:
    r = parse_rules(
        f"""
version: 1
forbidden:
  - id: first
    kind: path
    patterns: ['{vault}']
    reason: First.
  - id: second
    kind: path
    patterns: ['{vault.parent}']
    reason: Second.
"""
    )
    entry = r.forbidden_match(Target("path", str(vault / "secret.db")))
    assert entry is not None
    assert entry.id == "first"


def test_the_environment_is_read_at_load_not_at_match(
    monkeypatch: pytest.MonkeyPatch, vault: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("APPDATA", str(vault))
    r = one_rule("path", "%APPDATA%")
    monkeypatch.setenv("APPDATA", str(tmp_path / "elsewhere"))
    assert matched(r, str(vault / "secret.db"))
    assert os.environ["APPDATA"].endswith("elsewhere")
