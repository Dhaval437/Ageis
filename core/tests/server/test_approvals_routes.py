"""`POST /v1/approvals/{id}`, `GET /v1/rules`, `DELETE /v1/rules/{id}` (`P3-11`).

The broker's futures belong to the app's event loop, so these tests put a recording
fake on `app.state.approvals` and check what the routes do with its answers: the
status codes, the validation, and that nothing in a request can name a path.

An answer other than `deny` must carry `x-aegis-grant`, MAIN's HMAC over the exact
request (`P3-19`): MAIN signs only what its approval window sent, so the renderer's
generic `core.request` can deny a question and can never allow one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aegis_core.guardian.allow_rules import AllowRule
from aegis_core.guardian.approvals import (
    ApprovalBroker,
    ApprovalError,
    ApprovalNotFoundError,
    Resolution,
)
from aegis_core.server.app import create_app
from aegis_core.server.auth import GRANT_HEADER, grant_signature
from aegis_core.storage.allow_rules import AllowRuleStore
from aegis_core.storage.db import connect, migrate
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.server.test_auth import AUTHORIZED, OTHER_TOKEN, TOKEN, make_auth

ALLOW_REFUSED = "Only the approval dialog can allow this."


@dataclass
class FakeBroker:
    """Answers `resolve()` from a script; remembers the calls it got."""

    answers: dict[int, Resolution | ApprovalError] = field(default_factory=dict)
    calls: list[tuple[int, str, str | None]] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    rule_store: AllowRuleStore | None = None

    def resolve(self, approval_id: int, choice: str, rule: str | None = None) -> Resolution:
        self.calls.append((approval_id, choice, rule))
        answer = self.answers.get(
            approval_id, ApprovalNotFoundError("That approval was already answered.")
        )
        if isinstance(answer, ApprovalError):
            raise answer
        return answer

    def deny_all(self, decided_by: str = "stopped") -> int:
        self.denied.append(decided_by)
        return 0

    def close(self) -> None:
        pass


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AllowRuleStore]:
    conn = connect(tmp_path / "db" / "aegis.db", cross_thread=True)
    migrate(conn)
    with AllowRuleStore(conn) as opened:
        yield opened


def client_with(broker: FakeBroker) -> TestClient:
    app = create_app(make_auth(), approvals=broker)  # type: ignore[arg-type]
    return TestClient(app)


def answer(
    client: TestClient,
    approval_id: int | str,
    body: dict[str, str],
    *,
    token: str = TOKEN,
    sign_id: int | str | None = None,
    sign_body: dict[str, str] | None = None,
) -> Response:
    """Answer as MAIN does for its approval window: signed over the exact request."""
    content = json.dumps(body).encode()
    signature = grant_signature(
        token,
        "POST",
        f"/v1/approvals/{approval_id if sign_id is None else sign_id}",
        content if sign_body is None else json.dumps(sign_body).encode(),
    )
    headers = {**AUTHORIZED, GRANT_HEADER: signature, "content-type": "application/json"}
    return client.post(f"/v1/approvals/{approval_id}", content=content, headers=headers)


def test_an_answer_reaches_the_broker_and_comes_back() -> None:
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    with client_with(broker) as client:
        response = answer(client, 4, {"choice": "allow"})
    assert response.status_code == 200
    assert response.json() == {"approval_id": 4, "choice": "allow", "rule_id": None}
    assert broker.calls == [(4, "allow", None)]


def test_allow_always_returns_the_rule_it_made() -> None:
    rule = AllowRule(11, "exact", "fs.read_file", params_digest="0" * 64)
    broker = FakeBroker({4: Resolution(4, "allow_always", "user", rule)})
    with client_with(broker) as client:
        response = answer(client, 4, {"choice": "allow_always", "rule": "exact"})
    assert response.json()["rule_id"] == 11
    assert broker.calls == [(4, "allow_always", "exact")]


def test_an_approval_that_is_gone_is_a_404() -> None:
    with client_with(FakeBroker()) as client:
        response = answer(client, 9, {"choice": "allow"})
    assert response.status_code == 404
    assert "already answered" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Only MAIN's approval window can allow (`P3-19`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{"choice": "allow"}, {"choice": "allow_always", "rule": "exact"}],
    ids=["allow", "allow-always"],
)
def test_an_unsigned_allow_is_refused_and_never_reaches_the_broker(body: dict[str, str]) -> None:
    """Exactly what the main window's generic `core.request` would send."""
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    with client_with(broker) as client:
        response = client.post("/v1/approvals/4", headers=AUTHORIZED, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": ALLOW_REFUSED}
    assert broker.calls == []


def test_an_unsigned_deny_is_accepted() -> None:
    """Denying only ever makes the agent do less, so the HUD and a bare request may."""
    broker = FakeBroker({4: Resolution(4, "deny", "user")})
    with client_with(broker) as client:
        response = client.post("/v1/approvals/4", headers=AUTHORIZED, json={"choice": "deny"})
    assert response.status_code == 200
    assert broker.calls == [(4, "deny", None)]


@pytest.mark.parametrize(
    "tamper",
    ["wrong token", "another question", "a deny", "another rule", "a query added", "garbage"],
)
def test_a_signature_that_is_not_for_this_answer_is_refused(tamper: str) -> None:
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    body = {"choice": "allow_always", "rule": "exact"}
    with client_with(broker) as client:
        if tamper == "wrong token":
            response = answer(client, 4, body, token=OTHER_TOKEN)
        elif tamper == "another question":
            response = answer(client, 4, body, sign_id=5)
        elif tamper == "a deny":
            # Signed as a deny, which anyone may send, and sent as an allow.
            response = answer(client, 4, body, sign_body={"choice": "deny"})
        elif tamper == "another rule":
            response = answer(
                client, 4, body, sign_body={"choice": "allow_always", "rule": "tool_for_task"}
            )
        elif tamper == "a query added":
            response = answer(client, "4?x=1", body, sign_id=4)
        else:
            headers = {**AUTHORIZED, GRANT_HEADER: "zz" * 32}
            response = client.post("/v1/approvals/4", headers=headers, json=body)
    assert response.status_code == 403
    assert broker.calls == []


def test_a_refused_allow_leaves_the_question_open_for_the_real_answer() -> None:
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    with client_with(broker) as client:
        forged = client.post("/v1/approvals/4", headers=AUTHORIZED, json={"choice": "allow"})
        assert forged.status_code == 403
        assert answer(client, 4, {"choice": "allow"}).status_code == 200
    assert broker.calls == [(4, "allow", None)]


def test_a_refused_answer_is_a_400_with_the_sentence() -> None:
    broker = FakeBroker({4: ApprovalError("This action cannot be always allowed.")})
    with client_with(broker) as client:
        response = answer(client, 4, {"choice": "allow_always", "rule": "exact"})
    assert response.status_code == 400
    assert response.json()["detail"] == "This action cannot be always allowed."


@pytest.mark.parametrize(
    "body",
    [
        {"choice": "yes"},
        {"choice": "allow", "rule": "everything"},
        {"choice": "allow_always", "rule": "tool_in_folder", "folder": "C:\\"},
        {},
    ],
    ids=["bad-choice", "bad-rule", "a-path", "empty"],
)
def test_a_malformed_answer_is_refused_and_never_reaches_the_broker(body: dict[str, str]) -> None:
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    with client_with(broker) as client:
        response = client.post("/v1/approvals/4", headers=AUTHORIZED, json=body)
    assert response.status_code == 422
    assert broker.calls == []


@pytest.mark.parametrize("approval_id", ["0", "-1", "abc"])
def test_an_id_must_be_a_positive_integer(approval_id: str) -> None:
    broker = FakeBroker()
    with client_with(broker) as client:
        response = client.post(
            f"/v1/approvals/{approval_id}", headers=AUTHORIZED, json={"choice": "allow"}
        )
    assert response.status_code == 422
    assert broker.calls == []


def test_approvals_are_behind_session_auth() -> None:
    broker = FakeBroker({4: Resolution(4, "allow", "user")})
    with client_with(broker) as client:
        response = client.post("/v1/approvals/4", json={"choice": "allow"})
    assert response.status_code in (401, 403)
    assert broker.calls == []


def test_the_kill_switch_denies_every_pending_approval() -> None:
    broker = FakeBroker()
    with client_with(broker) as client:
        assert client.post("/v1/kill", headers=AUTHORIZED).status_code == 200
    assert broker.denied == ["stopped"]


def test_rules_are_listed_and_revoked(store: AllowRuleStore) -> None:
    kept = store.add("tool_for_task", "fs.read_file", task_id="t1")
    gone = store.add("tool_in_folder", "fs.read_file", folder="c:\\docs")
    broker = FakeBroker(rule_store=store)
    with client_with(broker) as client:
        listed = client.get("/v1/rules", headers=AUTHORIZED).json()["rules"]
        assert [r["id"] for r in listed] == [gone.id, kept.id]
        assert listed[0]["folder"] == "c:\\docs"
        after = client.delete(f"/v1/rules/{gone.id}", headers=AUTHORIZED)
        assert after.status_code == 200
        assert [r["id"] for r in after.json()["rules"]] == [kept.id]
        assert client.delete(f"/v1/rules/{gone.id}", headers=AUTHORIZED).status_code == 404


def test_with_no_rule_store_there_are_no_rules() -> None:
    with client_with(FakeBroker()) as client:
        assert client.get("/v1/rules", headers=AUTHORIZED).json() == {"rules": []}
        assert client.delete("/v1/rules/1", headers=AUTHORIZED).status_code == 404


def test_every_app_has_a_broker_even_when_none_is_given() -> None:
    with TestClient(create_app(make_auth())) as client:
        assert isinstance(client.app.state.approvals, ApprovalBroker)  # type: ignore[attr-defined]
        assert answer(client, 1, {"choice": "allow"}).status_code == 404
