"""`POST /v1/kill` — the kill switch's polite half (`P3-06`, `ARCHITECTURE.md § 9.1`)."""

from __future__ import annotations

from aegis_core.actuation.input import VK_LCONTROL, InputController
from aegis_core.actuation.killswitch import KillSwitch
from aegis_core.server.app import create_app
from aegis_core.server.schemas import KillResponse
from fastapi.testclient import TestClient

from tests.actuation.helpers import RecordingBackend
from tests.server.test_auth import AUTHORIZED, make_auth


def test_kill_engages_the_apps_switch_and_releases_held_keys() -> None:
    switch = KillSwitch()
    ctl = InputController(RecordingBackend(), kill=switch, inter_event_delay=0.0)
    ctl.key_down(VK_LCONTROL)

    with TestClient(create_app(make_auth(), kill_switch=switch)) as client:
        response = client.post("/v1/kill", headers=AUTHORIZED)

    assert response.status_code == 200
    body = KillResponse.model_validate(response.json())
    assert body.engaged is True
    assert (body.released_keys, body.released_buttons, body.release_failures) == (1, 0, 0)
    assert switch.engaged
    assert ctl.held_keys == ()


def test_every_app_has_a_switch_even_when_none_is_given() -> None:
    with TestClient(create_app(make_auth())) as client:
        response = client.post("/v1/kill", headers=AUTHORIZED)
        assert response.status_code == 200
        assert client.app.state.kill_switch.engaged  # type: ignore[attr-defined]


def test_kill_is_behind_session_auth() -> None:
    switch = KillSwitch()
    with TestClient(create_app(make_auth(), kill_switch=switch)) as client:
        response = client.post("/v1/kill")
    assert response.status_code in (401, 403)
    assert not switch.engaged


def test_kill_is_post_only() -> None:
    with TestClient(create_app(make_auth())) as client:
        assert client.get("/v1/kill", headers=AUTHORIZED).status_code == 405
