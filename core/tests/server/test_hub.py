"""Tests for `EventHub` — `seq`, replay, fan-out and its bounds (`ARCHITECTURE.md § 9.2`)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from aegis_core.server.hub import (
    CLOSE_GOING_AWAY,
    CLOSE_SUBSCRIBER_OVERFLOW,
    SUBSCRIBER_QUEUE_EVENTS,
    Closed,
    EventHub,
    ReplayUnavailableError,
    Subscription,
)
from aegis_core.server.schemas import StreamEvent


async def _drain(subscription: Subscription) -> list[StreamEvent | Closed]:
    """Everything currently owed to `subscription`, after pending callbacks run."""
    await asyncio.sleep(0)
    items: list[StreamEvent | Closed] = []
    while subscription.backlog or not subscription.queue.empty():
        items.append(await subscription.next())
    return items


def _seqs(items: list[StreamEvent | Closed]) -> list[int]:
    return [item.seq for item in items if isinstance(item, StreamEvent)]


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #


def test_seq_starts_at_one_and_increments() -> None:
    hub = EventHub()
    assert hub.last_seq == 0
    events = [hub.publish("log", {"n": n}) for n in range(3)]
    assert [e.seq for e in events] == [1, 2, 3]
    assert hub.last_seq == 3


def test_an_event_has_the_documented_shape() -> None:
    event = EventHub().publish("task.status", {"status": "running"}, task_id="t-1")
    assert set(event.model_dump()) == {"seq", "ts", "task_id", "type", "payload"}
    assert event.task_id == "t-1"
    assert event.type == "task.status"
    assert event.payload == {"status": "running"}
    assert event.ts.endswith("+00:00")


def test_an_undocumented_event_type_is_refused() -> None:
    with pytest.raises(ValueError, match="type"):
        EventHub().publish("task.exploded", {})  # type: ignore[arg-type]


def test_publishing_from_many_threads_never_reuses_a_seq() -> None:
    hub = EventHub(replay_limit=10_000)

    def burst() -> None:
        for _ in range(500):
            hub.publish("log")

    threads = [threading.Thread(target=burst) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert hub.last_seq == 4000


def test_the_replay_buffer_is_bounded() -> None:
    hub = EventHub(replay_limit=4)
    for _ in range(10):
        hub.publish("log")
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ReplayUnavailableError):
            hub.subscribe(loop, since=5)
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
# subscribe / replay
# --------------------------------------------------------------------------- #


async def test_a_fresh_subscriber_gets_everything_retained_then_live_events() -> None:
    hub = EventHub()
    hub.publish("log")
    hub.publish("log")
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    hub.publish("log")
    assert _seqs(await _drain(subscription)) == [1, 2, 3]


async def test_since_replays_only_what_came_after_it() -> None:
    hub = EventHub()
    for _ in range(5):
        hub.publish("log")
    subscription = hub.subscribe(asyncio.get_running_loop(), since=3)
    hub.publish("log")
    assert _seqs(await _drain(subscription)) == [4, 5, 6]


async def test_since_equal_to_the_latest_seq_replays_nothing() -> None:
    hub = EventHub()
    hub.publish("log")
    subscription = hub.subscribe(asyncio.get_running_loop(), since=1)
    assert await _drain(subscription) == []


async def test_since_zero_on_an_empty_hub_is_valid() -> None:
    subscription = EventHub().subscribe(asyncio.get_running_loop(), since=0)
    assert await _drain(subscription) == []


async def test_since_behind_the_evicted_window_is_refused_not_replayed_with_a_hole() -> None:
    hub = EventHub(replay_limit=3)
    for _ in range(6):
        hub.publish("log")  # retained: 4, 5, 6
    loop = asyncio.get_running_loop()
    with pytest.raises(ReplayUnavailableError):
        hub.subscribe(loop, since=2)  # 3 is gone
    assert _seqs(await _drain(hub.subscribe(loop, since=3))) == [4, 5, 6]


async def test_since_ahead_of_this_core_is_refused() -> None:
    """A cursor from a core that has since restarted must not be trusted."""
    hub = EventHub()
    hub.publish("log")
    with pytest.raises(ReplayUnavailableError):
        hub.subscribe(asyncio.get_running_loop(), since=40)


async def test_events_published_from_another_thread_arrive_in_order() -> None:
    hub = EventHub()
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    publisher = threading.Thread(target=lambda: [hub.publish("log") for _ in range(100)])
    publisher.start()
    await asyncio.to_thread(publisher.join)
    assert _seqs(await _drain(subscription)) == list(range(1, 101))


async def test_every_subscriber_gets_every_event() -> None:
    hub = EventHub()
    loop = asyncio.get_running_loop()
    first = hub.subscribe(loop, since=None)
    second = hub.subscribe(loop, since=None)
    hub.publish("log")
    assert _seqs(await _drain(first)) == [1]
    assert _seqs(await _drain(second)) == [1]


async def test_an_unsubscribed_client_receives_nothing_more() -> None:
    hub = EventHub()
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    hub.unsubscribe(subscription)
    hub.publish("log")
    assert await _drain(subscription) == []
    assert hub.subscriber_count == 0


# --------------------------------------------------------------------------- #
# bounds and shutdown
# --------------------------------------------------------------------------- #


async def test_a_subscriber_that_falls_too_far_behind_is_cut_off() -> None:
    hub = EventHub(replay_limit=SUBSCRIBER_QUEUE_EVENTS * 2)
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    for _ in range(SUBSCRIBER_QUEUE_EVENTS + 10):
        hub.publish("log")
    items = await _drain(subscription)
    assert _seqs(items) == list(range(1, SUBSCRIBER_QUEUE_EVENTS + 1))
    assert items[-1] == Closed(CLOSE_SUBSCRIBER_OVERFLOW)
    # ...and can catch up from the replay buffer with the last seq it saw.
    resumed = hub.subscribe(asyncio.get_running_loop(), since=SUBSCRIBER_QUEUE_EVENTS)
    assert _seqs(await _drain(resumed)) == list(
        range(SUBSCRIBER_QUEUE_EVENTS + 1, SUBSCRIBER_QUEUE_EVENTS + 11)
    )


async def test_a_large_replay_does_not_count_as_falling_behind() -> None:
    hub = EventHub(replay_limit=SUBSCRIBER_QUEUE_EVENTS * 3)
    for _ in range(SUBSCRIBER_QUEUE_EVENTS * 2):
        hub.publish("log")
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    hub.publish("log")
    items = await _drain(subscription)
    assert len(_seqs(items)) == SUBSCRIBER_QUEUE_EVENTS * 2 + 1
    assert not any(isinstance(item, Closed) for item in items)


async def test_close_ends_every_stream_with_going_away() -> None:
    hub = EventHub()
    subscription = hub.subscribe(asyncio.get_running_loop(), since=None)
    hub.close()
    assert await _drain(subscription) == [Closed(CLOSE_GOING_AWAY)]
    assert hub.subscriber_count == 0


def test_a_subscriber_on_a_closed_loop_is_pruned_on_publish() -> None:
    hub = EventHub()
    loop = asyncio.new_event_loop()
    hub.subscribe(loop, since=None)
    loop.close()
    hub.publish("log")
    assert hub.subscriber_count == 0
