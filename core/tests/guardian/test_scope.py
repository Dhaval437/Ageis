"""Tests for `guardian/scope.py`, the `scopes` table, and scope enforcement (`P3-10`)."""

from __future__ import annotations

import _winapi
import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from aegis_core.guardian.policy import Guardian, GuardianDeniedError, TaskContext
from aegis_core.guardian.rules import Rules, Target, canonical_path, parse_rules
from aegis_core.guardian.scope import (
    EMPTY,
    MAX_APPS,
    MAX_FOLDERS,
    Scope,
    ScopeError,
    load_scope,
    make_scope,
)
from aegis_core.server.schemas import RiskTier
from aegis_core.storage.db import StorageError, connect, migrate
from aegis_core.storage.scopes import ScopeRecord, ScopeStore
from pydantic import JsonValue

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    place = tmp_path / "Work Files ü"
    (place / "reports").mkdir(parents=True)
    return place


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    place = tmp_path / "Secrets"
    place.mkdir()
    return place


@pytest.fixture
def rules(secrets: Path) -> Rules:
    return parse_rules(
        "version: 1\nforbidden:\n  - id: secrets\n    kind: path\n"
        f"    patterns: ['{secrets}']\n    reason: Never the secrets.\n"
    )


def c(path: Path | str) -> str:
    canonical = canonical_path(str(path))
    assert canonical is not None
    return canonical


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #


def test_a_folder_contains_itself_and_what_is_inside(work: Path, rules: Rules) -> None:
    scope = make_scope("Work", [str(work)], rules)
    assert scope.contains(c(work))
    assert scope.contains(c(work / "reports" / "q3.xlsx"))
    assert scope.contains(c(work / "not-yet" / "deep" / "new.txt"))


def test_a_sibling_that_shares_a_prefix_is_outside(work: Path, rules: Rules) -> None:
    (work.parent / "Work Files üx").mkdir()
    scope = make_scope("Work", [str(work)], rules)
    assert not scope.contains(c(work.parent / "Work Files üx" / "a.txt"))
    assert not scope.contains(c(work.parent))


def test_the_empty_scope_contains_nothing(work: Path) -> None:
    assert not EMPTY.contains(c(work))


# --------------------------------------------------------------------------- #
# What may be a scope folder
# --------------------------------------------------------------------------- #


def test_folders_are_stored_canonical(work: Path, rules: Rules) -> None:
    spelled = str(work / "reports" / "..").upper()
    assert make_scope("Work", [spelled], rules).folders == (c(work),)


def test_a_junction_is_stored_as_where_it_points(work: Path, tmp_path: Path, rules: Rules) -> None:
    link = tmp_path / "shortcut"
    _winapi.CreateJunction(str(work), str(link))
    assert make_scope("Work", [str(link)], rules).folders == (c(work),)


@pytest.mark.parametrize(
    "folder",
    [os.environ.get("SYSTEMDRIVE", "C:") + "\\", "D:\\", "\\\\fileserver\\share"],
    ids=["system-drive", "other-drive", "share-root"],
)
def test_a_whole_drive_or_share_is_never_a_scope(folder: str, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="whole drive"):
        make_scope("All", [folder], rules)


def test_a_folder_on_a_share_may_be_a_scope(rules: Rules) -> None:
    scope = make_scope("Team", ["\\\\fileserver\\share\\team"], rules)
    assert scope.folders == ("\\\\fileserver\\share\\team",)


@pytest.mark.parametrize("why", ["missing", "file"])
def test_a_folder_must_exist_and_be_a_folder(tmp_path: Path, rules: Rules, why: str) -> None:
    target = tmp_path / "nothing"
    if why == "file":
        target.write_text("x")
    with pytest.raises(ScopeError, match="does not exist"):
        make_scope("X", [str(target)], rules)


def test_a_forbidden_folder_is_refused(secrets: Path, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="never touches"):
        make_scope("X", [str(secrets)], rules)
    (secrets / "inner").mkdir()
    with pytest.raises(ScopeError, match="never touches"):
        make_scope("X", [str(secrets / "inner")], rules)


def test_a_folder_holding_a_forbidden_one_is_allowed_but_the_rule_still_wins(
    tmp_path: Path, secrets: Path, rules: Rules
) -> None:
    scope = make_scope("Everything here", [str(tmp_path)], rules)
    guardian = Guardian(rules)
    tool = Touching(Target("path", str(secrets / "a.txt"), "write"))
    verdict = guardian.evaluate(tool, {}, TaskContext("trusted", in_scope=scope.contains))
    assert (verdict.decision, verdict.stage) == ("deny", "forbidden")


@pytest.mark.parametrize(
    "path",
    ["relative\\folder", "\\\\.\\C:\\Users", "\\\\localhost\\C$\\Users"],
    ids=["relative", "device", "admin-share"],
)
def test_a_folder_that_cannot_be_checked_is_refused(path: str, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="could not check"):
        make_scope("X", [path], rules)


def test_nesting_and_duplicates_collapse(work: Path, rules: Rules) -> None:
    scope = make_scope(
        "Work", [str(work / "reports"), str(work), str(work).upper(), str(work / "reports")], rules
    )
    assert scope.folders == (c(work),)


def test_errors_never_quote_the_path(tmp_path: Path, rules: Rules) -> None:
    marker = tmp_path / "ZZmarkerZZ"
    with pytest.raises(ScopeError) as error:
        make_scope("X", [str(marker)], rules)
    assert "zzmarkerzz" not in str(error.value).lower()


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "bad\nname"])
def test_a_scope_needs_a_real_name(name: str, work: Path, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="name"):
        make_scope(name, [str(work)], rules)


def test_folders_and_apps_are_capped(work: Path, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="folders"):
        make_scope("X", [str(work)] * (MAX_FOLDERS + 1), rules)
    with pytest.raises(ScopeError, match="apps"):
        make_scope("X", [str(work)], rules, apps=[f"a{i}.exe" for i in range(MAX_APPS + 1)])


def test_apps_are_program_file_names(work: Path, rules: Rules) -> None:
    scope = make_scope(
        "Work", [str(work)], rules, apps=["EXCEL.EXE", "C:\\Tools\\notepad++.exe", "excel.exe"]
    )
    assert scope.apps == ("excel.exe", "notepad++.exe")
    assert scope.admits_app("Excel.exe")
    assert scope.admits_app("C:\\Program Files\\Microsoft Office\\EXCEL.EXE")
    assert not scope.admits_app("powershell.exe")


@pytest.mark.parametrize("app", ["excel", "run.bat", "", "a<b.exe", "x\x01.exe"])
def test_an_app_that_is_not_a_program_file_is_refused(app: str, work: Path, rules: Rules) -> None:
    with pytest.raises(ScopeError, match="program file"):
        make_scope("X", [str(work)], rules, apps=[app])


# --------------------------------------------------------------------------- #
# Stored, and re-checked on the way back
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ScopeStore]:
    conn = connect(tmp_path / "db" / "aegis.db", cross_thread=True)
    migrate(conn)
    with ScopeStore(conn) as opened:
        yield opened


def test_a_scope_round_trips(store: ScopeStore, work: Path, rules: Rules) -> None:
    made = make_scope("Work", [str(work)], rules, apps=["excel.exe"])
    scope_id = store.save(made.name, made.folders, made.apps, None)
    record = store.get(scope_id)
    assert record is not None
    assert load_scope(record, rules) == Scope("Work", made.folders, made.apps, scope_id)
    assert [r.id for r in store.list()] == [scope_id]


def test_a_scope_can_be_renamed_and_removed(store: ScopeStore, work: Path) -> None:
    scope_id = store.save("Work", (c(work),), (), None)
    assert store.save("Office", (c(work),), (), scope_id) == scope_id
    record = store.get(scope_id)
    assert record is not None
    assert record.name == "Office"
    assert store.delete(scope_id)
    assert store.get(scope_id) is None
    with pytest.raises(StorageError, match="no longer exists"):
        store.save("Gone", (), (), scope_id)


def test_names_are_unique_ignoring_case(store: ScopeStore) -> None:
    store.save("Work Files", (), (), None)
    with pytest.raises(StorageError, match="already has that name"):
        store.save("work files", (), (), None)


def test_a_folder_deleted_since_is_dropped_on_load(
    store: ScopeStore, work: Path, tmp_path: Path, rules: Rules
) -> None:
    other = tmp_path / "Other"
    other.mkdir()
    scope_id = store.save("Work", (c(work), c(other)), (), None)
    shutil.rmtree(other)
    record = store.get(scope_id)
    assert record is not None
    assert load_scope(record, rules).folders == (c(work),)


def test_a_stored_row_can_never_widen_a_scope(tmp_path: Path, secrets: Path, rules: Rules) -> None:
    # Written by hand (or by an older build): a drive root, a forbidden folder, junk.
    record = ScopeRecord(
        id=7,
        name="Tampered",
        folders=("C:\\", str(secrets), "relative", "\\\\localhost\\c$\\x", str(tmp_path)),
        apps=("cmd", "excel.exe"),
    )
    scope = load_scope(record, rules)
    assert scope.folders == (c(tmp_path),)
    assert scope.apps == ("excel.exe",)


def test_an_unreadable_row_is_skipped(store: ScopeStore, tmp_path: Path) -> None:
    store.save("Good", (), (), None)
    conn = sqlite3.connect(tmp_path / "db" / "aegis.db")
    conn.execute(
        "INSERT INTO scopes (name, folders_json, apps_json, created_at, updated_at)"
        " VALUES ('Mixed', '[1, \"C:\\\\x\", null]', '[]', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    names = {record.name: record for record in store.list()}
    assert names["Mixed"].folders == ("C:\\x",)


def test_the_table_refuses_what_is_not_a_list(store: ScopeStore, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "db" / "aegis.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scopes (name, folders_json, apps_json, created_at, updated_at)"
            " VALUES ('Bad', '{\"a\": 1}', '[]', 'x', 'x')"
        )
    conn.close()


# --------------------------------------------------------------------------- #
# Enforcement: evaluate() plans, check_target() re-checks at the moment of acting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Touching:
    target: Target
    name: str = "fs.write_file"
    risk: RiskTier = "CAUTION"

    def targets(self, params: Mapping[str, JsonValue]) -> Sequence[Target]:
        return (self.target,)


def ctx_for(scope: Scope) -> TaskContext:
    return TaskContext("standard", in_scope=scope.contains)


def test_writing_in_scope_is_allowed_and_outside_is_denied(
    work: Path, tmp_path: Path, rules: Rules
) -> None:
    scope = make_scope("Work", [str(work)], rules)
    guardian = Guardian(rules)
    inside = Touching(Target("path", str(work / "reports" / "q3.xlsx")))
    outside = Touching(Target("path", str(tmp_path / "Elsewhere" / "x.txt")))
    assert guardian.evaluate(inside, {}, ctx_for(scope)).decision == "allow"
    assert guardian.evaluate(outside, {}, ctx_for(scope)).stage == "scope"


def test_a_junction_inside_the_scope_that_leads_out_is_outside(
    work: Path, tmp_path: Path, rules: Rules
) -> None:
    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    _winapi.CreateJunction(str(elsewhere), str(work / "door"))
    scope = make_scope("Work", [str(work)], rules)
    tool = Touching(Target("path", str(work / "door" / "x.txt")))
    verdict = Guardian(rules).evaluate(tool, {}, ctx_for(scope))
    assert (verdict.decision, verdict.stage) == ("deny", "scope")


def test_dot_dot_out_of_the_scope_is_outside(work: Path, rules: Rules) -> None:
    scope = make_scope("Work", [str(work)], rules)
    tool = Touching(Target("path", str(work) + "\\reports\\..\\..\\x.txt"))
    assert Guardian(rules).evaluate(tool, {}, ctx_for(scope)).stage == "scope"


def test_check_target_returns_the_canonical_path_to_act_on(work: Path, rules: Rules) -> None:
    scope = make_scope("Work", [str(work)], rules)
    spelled = str(work / "reports" / ".." / "reports" / "q3.xlsx").upper()
    got = Guardian(rules).check_target(Target("path", spelled), ctx_for(scope))
    assert got == c(work / "reports" / "q3.xlsx")


def test_a_folder_swapped_for_a_junction_after_approval_is_caught(
    work: Path, tmp_path: Path, rules: Rules
) -> None:
    # The race `check_target()` exists for: the plan was fine, then the disk changed.
    scope = make_scope("Work", [str(work)], rules)
    guardian = Guardian(rules)
    target = Target("path", str(work / "reports" / "q3.xlsx"))
    assert guardian.evaluate(Touching(target), {}, ctx_for(scope)).decision == "allow"

    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(work / "reports")
    _winapi.CreateJunction(str(elsewhere), str(work / "reports"))

    with pytest.raises(GuardianDeniedError, match="outside the task's scope"):
        guardian.check_target(target, ctx_for(scope))


def test_check_target_refuses_what_is_forbidden_or_unreadable(
    secrets: Path, work: Path, rules: Rules
) -> None:
    ctx = TaskContext("trusted", in_scope=lambda _path: True)
    guardian = Guardian(rules)
    with pytest.raises(GuardianDeniedError, match="Never the secrets"):
        guardian.check_target(Target("path", str(secrets / "a"), "read"), ctx)
    with pytest.raises(GuardianDeniedError, match="could not check"):
        guardian.check_target(Target("path", "relative.txt"), ctx)


def test_check_target_lets_an_approved_read_outside_the_scope_through(
    work: Path, tmp_path: Path, rules: Rules
) -> None:
    # A read outside the scope was asked about when it was planned; asking was the check.
    scope = make_scope("Work", [str(work)], rules)
    target = Target("path", str(tmp_path / "manual.pdf"), "read")
    assert Guardian(rules).check_target(target, ctx_for(scope)) == c(tmp_path / "manual.pdf")
