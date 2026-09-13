"""End-to-end tests for `WS /v1/stream` through the authenticated app (`§ 9.2`).

`TestClient` runs the app on its own thread, so every `hub.publish` here is also a
cross-thread publish — the same shape as the agent loop publishing in production.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from aegis_core.server.app import create_app
from aegis_core.server.handshake import bind_loopback
from aegis_core.server.hub import (
    CLOSE_INVALID_SINCE,
    CLOSE_REPLAY_UNAVAILABLE,
    CLOSE_UNSUPPORTED_DATA,
    EventHub,
)
from aegis_core.server.schemas import StreamEvent
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from tests.server.test_auth import AUTHORIZED, OTHER_TOKEN, make_auth


@pytest.fixture
def hub() -> EventHub:
    return EventHub(replay_limit=8)


@pytest.fixture
def client(hub: EventHub) -> Iterator[TestClient]:
    with TestClient(create_app(make_auth(), hub)) as test_client:
        yield test_client


def _authorized() -> dict[str, str]:
    """A fresh copy: starlette's `websocket_connect` writes its handshake headers into
    the dict it is given, which would corrupt the shared constant for later tests."""
    return dict(AUTHORIZED)


def _event(raw: str) -> StreamEvent:
    return StreamEvent.model_validate_json(raw)


def test_live_events_stream_as_json_in_seq_order(client: TestClient, hub: EventHub) -> None:
    with client.websocket_connect("/v1/stream", headers=_authorized()) as ws:
        hub.publish("task.created", {"goal": "sort the invoices"}, task_id="t-1")
        hub.publish("task.status", {"status": "running"}, task_id="t-1")
        first, second = _event(ws.receive_text()), _event(ws.receive_text())
    assert (first.seq, first.type, first.task_id) == (1, "task.created", "t-1")
    assert first.payload == {"goal": "sort the invoices"}
    assert (second.seq, second.type) == (2, "task.status")


def test_reconnecting_with_since_replays_what_was_missed(client: TestClient, hub: EventHub) -> None:
    with client.websocket_connect("/v1/stream", headers=_authorized()) as ws:
        hub.publish("log")
        last_seen = _event(ws.receive_text()).seq
    hub.publish("log")
    hub.publish("log")
    with client.websocket_connect(f"/v1/stream?since={last_seen}", headers=_authorized()) as ws:
        replayed = [_event(ws.receive_text()).seq for _ in range(2)]
        hub.publish("log")
        live = _event(ws.receive_text()).seq
    assert replayed == [2, 3]
    assert live == 4


def test_a_fresh_connection_replays_everything_retained(client: TestClient, hub: EventHub) -> None:
    for _ in range(3):
        hub.publish("log")
    with client.websocket_connect("/v1/stream", headers=_authorized()) as ws:
        assert [_event(ws.receive_text()).seq for _ in range(3)] == [1, 2, 3]


def test_an_unreplayable_since_closes_with_4410(client: TestClient, hub: EventHub) -> None:
    for _ in range(20):
        hub.publish("log")  # replay_limit=8, so 1..12 are gone
    with (
        client.websocket_connect("/v1/stream?since=2", headers=_authorized()) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_text()
    assert closed.value.code == CLOSE_REPLAY_UNAVAILABLE


@pytest.mark.parametrize("since", ["-1", "abc", "1.5", "", "9" * 16])
def test_a_malformed_since_closes_with_4400(client: TestClient, since: str) -> None:
    with (
        client.websocket_connect(f"/v1/stream?since={since}", headers=_authorized()) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_text()
    assert closed.value.code == CLOSE_INVALID_SINCE


def test_a_client_message_closes_the_stream(client: TestClient, hub: EventHub) -> None:
    """The stream is one-way; commands go over REST."""
    with client.websocket_connect("/v1/stream", headers=_authorized()) as ws:
        ws.send_text("pause please")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == CLOSE_UNSUPPORTED_DATA


def test_a_disconnect_unsubscribes(client: TestClient, hub: EventHub) -> None:
    with client.websocket_connect("/v1/stream", headers=_authorized()) as ws:
        hub.publish("log")
        ws.receive_text()
        assert hub.subscriber_count == 1
    hub.publish("log")  # nothing is listening; must not raise or buffer
    with client.websocket_connect("/v1/stream?since=2", headers=_authorized()):
        assert hub.subscriber_count == 1


# --------------------------------------------------------------------------- #
# The stream is behind the same session auth as every route
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {OTHER_TOKEN}"},
        {**_authorized(), "Origin": "http://evil.example"},
    ],
    ids=["no-token", "wrong-token", "origin-present"],
)
def test_an_unauthenticated_upgrade_is_refused(
    client: TestClient, hub: EventHub, headers: dict[str, str]
) -> None:
    hub.publish("log", {"secret": "must not leak"})
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/v1/stream", headers=headers),
    ):
        pass
    assert hub.subscriber_count == 0


def test_an_untrusted_peer_cannot_subscribe(hub: EventHub) -> None:
    app = create_app(make_auth(resolve_peer=lambda _local, _peer: 1), hub)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/v1/stream", headers=_authorized()),
    ):
        pass
    assert hub.subscriber_count == 0


# --------------------------------------------------------------------------- #
# Over a real socket: uvicorn + the `websockets` protocol, not TestClient
# --------------------------------------------------------------------------- #


def test_events_cross_a_real_loopback_socket_in_order(hub: EventHub) -> None:
    """Guards the dependency, not just the handler: without `websockets` installed,
    uvicorn cannot upgrade the connection at all and TestClient would never notice."""
    sock = bind_loopback(0)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(make_auth(), hub), log_config=None))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        for n in range(3):
            hub.publish("log", {"n": n})

        async def read_six() -> list[StreamEvent]:
            url = f"ws://127.0.0.1:{port}/v1/stream?since=0"
            async with ws_connect(url, additional_headers=_authorized(), open_timeout=5) as ws:
                received = [_event(str(await ws.recv())) for _ in range(3)]
                publisher = threading.Thread(
                    target=lambda: [hub.publish("log", {"n": n}) for n in range(3, 6)]
                )
                publisher.start()
                received += [_event(str(await ws.recv())) for _ in range(3)]
                publisher.join()
                return received

        events = asyncio.run(asyncio.wait_for(read_six(), timeout=10))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    assert [e.payload["n"] for e in events] == [0, 1, 2, 3, 4, 5]
