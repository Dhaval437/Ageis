"""The scope routes (`P3-17`): who may put a folder in a scope, and what can only narrow.

The rule under test: **a folder enters a scope only because the person picked it in the
OS dialog.** The renderer reaches the core through MAIN with MAIN's own token, so the two
routes that add a folder also need `x-aegis-grant` — MAIN's HMAC over the exact request —
and every way of getting that wrong is a 403 that changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from aegis_core.guardian.rules import Rules, canonical_path, load_rules
from aegis_core.guardian.scope import ScopeBook
from aegis_core.server.app import create_app
from aegis_core.server.auth import GRANT_HEADER, grant_signature
from aegis_core.storage.db import connect, migrate
from aegis_core.storage.scopes import ScopeStore
from fastapi.testclient import TestClient

from tests.server.test_auth import AUTHORIZED, OTHER_TOKEN, TOKEN, make_auth

GRANT_REFUSED = "A folder can only be added by choosing it yourself."


@pytest.fixture(scope="module")
def rules() -> Rules:
    return load_rules()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ScopeStore]:
    conn = connect(tmp_path / "db" / "aegis.db", cross_thread=True)
    migrate(conn)
    with ScopeStore(conn) as opened:
        yield opened


@pytest.fixture
def client(store: ScopeStore, rules: Rules) -> Iterator[TestClient]:
    with TestClient(create_app(make_auth(), scopes=ScopeBook(store, rules))) as test_client:
        yield test_client


@pytest.fixture
def work(tmp_path: Path) -> Path:
    folder = tmp_path / "Work"
    folder.mkdir()
    return folder


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    folder = tmp_path / "Photos"
    folder.mkdir()
    return folder


def encoded(body: object) -> bytes:
    return json.dumps(body).encode()


def signed(
    client: TestClient,
    method: str,
    path: str,
    body: object,
    *,
    token: str = TOKEN,
    sign_path: str | None = None,
    sign_body: object | None = None,
) -> object:
    """Send `body` as MAIN would, signed over `sign_path`/`sign_body` (default: the same)."""
    content = encoded(body)
    signature = grant_signature(
        token,
        method,
        path if sign_path is None else sign_path,
        content if sign_body is None else encoded(sign_body),
    )
    headers = {**AUTHORIZED, GRANT_HEADER: signature, "content-type": "application/json"}
    return client.request(method, path, content=content, headers=headers)


def create(client: TestClient, name: str, folder: Path | str) -> dict[str, object]:
    response = signed(client, "POST", "/v1/scopes", {"name": name, "folder": str(folder)})
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    return response.json()  # type: ignore[attr-defined,no-any-return]


def listed(client: TestClient) -> list[dict[str, object]]:
    response = client.get("/v1/scopes", headers=AUTHORIZED)
    assert response.status_code == 200
    return response.json()["scopes"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# The signature
# ---------------------------------------------------------------------------


def test_the_signature_matches_mains(client: TestClient) -> None:
    """The same vector is asserted in `apps/desktop/tests/core-gateway.test.ts`."""
    body = b'{"name":"Work","folder":"C:\\\\Work"}'
    assert (
        grant_signature("a" * 64, "POST", "/v1/scopes", body)
        == "13701eb1a628d0c260bd25faf1d5e1ea14e7dd49b9ab42fcd7460e09b1f89ffa"
    )


def test_a_signed_create_makes_a_scope_of_the_canonical_folder(
    client: TestClient, work: Path
) -> None:
    scope = create(client, "Work files", work)
    assert scope["name"] == "Work files"
    assert scope["folders"] == [canonical_path(str(work))]
    assert scope["apps"] == []
    assert listed(client) == [scope]


def test_an_unsigned_create_is_refused_and_stores_nothing(client: TestClient, work: Path) -> None:
    """Exactly what the renderer's generic `core.request` would send."""
    response = client.post(
        "/v1/scopes", headers=AUTHORIZED, json={"name": "Mine", "folder": str(work)}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": GRANT_REFUSED}
    assert listed(client) == []


@pytest.mark.parametrize(
    "tamper",
    ["wrong token", "another folder", "another path", "a query added", "garbage", "empty"],
)
def test_a_signature_that_is_not_for_this_request_is_refused(
    client: TestClient, work: Path, photos: Path, tamper: str
) -> None:
    body = {"name": "Mine", "folder": str(work)}
    path = "/v1/scopes"
    if tamper == "wrong token":
        response = signed(client, "POST", path, body, token=OTHER_TOKEN)
    elif tamper == "another folder":
        # Signed for a folder the person picked; sent with a different one.
        response = signed(client, "POST", path, body, sign_body={**body, "folder": str(photos)})
    elif tamper == "another path":
        response = signed(client, "POST", path, body, sign_path="/v1/scopes/1/folders")
    elif tamper == "a query added":
        response = signed(client, "POST", f"{path}?x=1", body, sign_path=path)
    else:
        headers = {**AUTHORIZED, GRANT_HEADER: "zz" * 32 if tamper == "garbage" else ""}
        response = client.post(path, headers=headers, json=body)
    assert response.status_code == 403  # type: ignore[attr-defined]
    assert listed(client) == []


def test_a_percent_encoded_route_is_not_a_way_around_it(client: TestClient, work: Path) -> None:
    response = client.post(
        "/v1/%73copes", headers=AUTHORIZED, json={"name": "Mine", "folder": str(work)}
    )
    assert response.status_code in (403, 404)
    assert listed(client) == []


# ---------------------------------------------------------------------------
# Adding a folder
# ---------------------------------------------------------------------------


def test_a_signed_folder_is_added(client: TestClient, work: Path, photos: Path) -> None:
    scope = create(client, "Mine", work)
    response = signed(client, "POST", f"/v1/scopes/{scope['id']}/folders", {"folder": str(photos)})
    assert response.status_code == 200  # type: ignore[attr-defined]
    folders = response.json()["folders"]  # type: ignore[attr-defined]
    assert sorted(folders) == sorted(
        [str(canonical_path(str(work))), str(canonical_path(str(photos)))]
    )


def test_an_unsigned_folder_is_refused(client: TestClient, work: Path, photos: Path) -> None:
    scope = create(client, "Mine", work)
    response = client.post(
        f"/v1/scopes/{scope['id']}/folders", headers=AUTHORIZED, json={"folder": str(photos)}
    )
    assert response.status_code == 403
    assert listed(client)[0]["folders"] == [canonical_path(str(work))]


def test_a_folder_for_a_scope_that_is_gone_is_a_404(client: TestClient, work: Path) -> None:
    response = signed(client, "POST", "/v1/scopes/99/folders", {"folder": str(work)})
    assert response.status_code == 404  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# What a scope folder may not be — `ScopeError`'s sentence, as a 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "sentence"),
    [
        ("C:\\", "A whole drive cannot be a scope. Choose a folder on it."),
        (
            "C:\\Windows\\System32\\config",
            "That folder is one Aegis never touches, so it cannot be in a scope.",
        ),
        ("C:\\no\\such\\folder\\anywhere", "That folder does not exist."),
        ("\\\\.\\C:\\Users", "That folder cannot be used: Aegis could not check where it points."),
    ],
    ids=["drive", "forbidden", "missing", "device"],
)
def test_a_folder_that_cannot_be_a_scope_is_refused_even_signed(
    client: TestClient, folder: str, sentence: str
) -> None:
    response = signed(client, "POST", "/v1/scopes", {"name": "Mine", "folder": folder})
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert response.json() == {"detail": sentence}  # type: ignore[attr-defined]
    assert listed(client) == []


def test_a_name_another_scope_has_is_a_400(client: TestClient, work: Path, photos: Path) -> None:
    create(client, "Mine", work)
    response = signed(client, "POST", "/v1/scopes", {"name": "Mine", "folder": str(photos)})
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert response.json() == {"detail": "Another scope already has that name."}  # type: ignore[attr-defined]


def test_a_blank_name_is_a_400(client: TestClient, work: Path) -> None:
    response = signed(client, "POST", "/v1/scopes", {"name": "  ", "folder": str(work)})
    assert response.status_code == 400  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# What can only narrow: rename, drop folders, delete
# ---------------------------------------------------------------------------


def test_put_renames_and_drops_folders(client: TestClient, work: Path, photos: Path) -> None:
    scope = create(client, "Mine", work)
    signed(client, "POST", f"/v1/scopes/{scope['id']}/folders", {"folder": str(photos)})
    keep = canonical_path(str(photos))
    response = client.put(
        f"/v1/scopes/{scope['id']}",
        headers=AUTHORIZED,
        json={"name": "Pictures", "folders": [keep]},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Pictures"
    assert response.json()["folders"] == [keep]


@pytest.mark.parametrize("spelling", ["new", "raw", "upper"])
def test_put_cannot_add_a_folder_in_any_spelling(
    client: TestClient, work: Path, photos: Path, spelling: str
) -> None:
    """Only a folder the scope already lists, exactly as it lists it, may be kept."""
    scope = create(client, "Mine", work)
    current = canonical_path(str(work))
    assert current is not None
    extra = {
        "new": str(photos),
        "raw": str(photos) + "\\",
        "upper": canonical_path(str(photos)).upper(),  # type: ignore[union-attr]
    }[spelling]
    response = client.put(
        f"/v1/scopes/{scope['id']}",
        headers=AUTHORIZED,
        json={"name": "Mine", "folders": [current, extra]},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": GRANT_REFUSED}
    assert listed(client)[0]["folders"] == [current]


def test_put_may_empty_a_scope(client: TestClient, work: Path) -> None:
    scope = create(client, "Mine", work)
    response = client.put(
        f"/v1/scopes/{scope['id']}", headers=AUTHORIZED, json={"name": "Mine", "folders": []}
    )
    assert response.status_code == 200
    assert response.json()["folders"] == []


def test_delete_removes_a_scope_and_answers_with_the_rest(
    client: TestClient, work: Path, photos: Path
) -> None:
    first = create(client, "A", work)
    create(client, "B", photos)
    response = client.delete(f"/v1/scopes/{first['id']}", headers=AUTHORIZED)
    assert response.status_code == 200
    assert [scope["name"] for scope in response.json()["scopes"]] == ["B"]
    assert client.delete(f"/v1/scopes/{first['id']}", headers=AUTHORIZED).status_code == 404


# ---------------------------------------------------------------------------
# Reading back narrows; a core without the parts says so
# ---------------------------------------------------------------------------


def test_a_folder_deleted_from_disk_is_no_longer_listed(
    client: TestClient, work: Path, photos: Path
) -> None:
    scope = create(client, "Mine", work)
    signed(client, "POST", f"/v1/scopes/{scope['id']}/folders", {"folder": str(photos)})
    photos.rmdir()
    assert listed(client)[0]["folders"] == [canonical_path(str(work))]


def test_without_a_scope_book_the_routes_are_503() -> None:
    with TestClient(create_app(make_auth())) as test_client:
        assert test_client.get("/v1/scopes", headers=AUTHORIZED).status_code == 503
