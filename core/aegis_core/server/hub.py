"""The live event bus behind `WS /v1/stream` (`ARCHITECTURE.md § 9.2`).

`EventHub.publish()` stamps an event with the next `seq` and fans it out to every
connected subscriber. It may be called from **any thread** — the agent loop and the
preemption watcher do not run on the event loop — and it never blocks on a
subscriber: a slow renderer must not be able to stall the agent.

Replay is from memory for now: the last `REPLAY_BUFFER_EVENTS` events are kept, and a
reconnecting client that sends `?since=<seq>` gets everything after it. SQLite replay
replaces the buffer in P6-06. Every bound is explicit, because `REVIEW.md § 2` forbids
an unbounded buffer:

- the replay buffer holds a fixed number of events;
- each subscriber's queue holds a fixed number of events, and a subscriber that falls
  that far behind is **disconnected** (`CLOSE_SUBSCRIBER_OVERFLOW`) rather than
  buffered for — it reconnects with `?since=` and catches up from the replay buffer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue

from aegis_core.server.schemas import EventType, StreamEvent

log = logging.getLogger(__name__)

#: How many past events a reconnecting client can catch up on.
REPLAY_BUFFER_EVENTS: Final = 2048

#: How far one subscriber may fall behind before it is disconnected.
SUBSCRIBER_QUEUE_EVENTS: Final = 512

#: `?since=` was not a non-negative integer.
CLOSE_INVALID_SINCE: Final = 4400
#: The events after `since` are not held by this core — evicted, or `since` came from
#: a core that has since restarted. Drop client state and reconnect **without** `since`.
CLOSE_REPLAY_UNAVAILABLE: Final = 4410
#: The subscriber fell `SUBSCRIBER_QUEUE_EVENTS` behind. Reconnect with `since`.
CLOSE_SUBSCRIBER_OVERFLOW: Final = 4429
#: The client sent a message. The stream is one-way; commands go over REST.
CLOSE_UNSUPPORTED_DATA: Final = 1003
#: The core is shutting down.
CLOSE_GOING_AWAY: Final = 1001


class ReplayUnavailableError(Exception):
    """`since` names a point this core cannot replay from."""


@dataclass(frozen=True)
class Closed:
    """Queued after the last event a subscriber will receive."""

    code: int


@dataclass(eq=False)
class Subscription:
    """One connected client: the events owed to it, in `seq` order.

    `backlog` is the replay snapshot taken at subscribe time and is drained first;
    it is bounded by the replay buffer. `queue` holds live events and is only ever
    touched on `loop`. It has one slot more than `SUBSCRIBER_QUEUE_EVENTS` so a
    `Closed` marker always fits.
    """

    loop: asyncio.AbstractEventLoop
    backlog: deque[StreamEvent] = field(default_factory=deque)
    queue: asyncio.Queue[StreamEvent | Closed] = field(
        default_factory=lambda: asyncio.Queue(SUBSCRIBER_QUEUE_EVENTS + 1)
    )
    closed: bool = False

    def deliver(self, item: StreamEvent | Closed) -> None:
        """Enqueue one live item. Must run on `loop`."""
        if self.closed:
            return
        if isinstance(item, Closed):
            self.closed = True
            self.queue.put_nowait(item)
            return
        if self.queue.qsize() >= SUBSCRIBER_QUEUE_EVENTS:
            log.warning("stream.subscriber_overflow", extra={"seq": item.seq})
            self.closed = True
            self.queue.put_nowait(Closed(CLOSE_SUBSCRIBER_OVERFLOW))
            return
        self.queue.put_nowait(item)

    async def next(self) -> StreamEvent | Closed:
        if self.backlog:
            return self.backlog.popleft()
        return await self.queue.get()


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds")


class EventHub:
    """Assigns `seq`, retains recent events, and fans them out."""

    def __init__(self, *, replay_limit: int = REPLAY_BUFFER_EVENTS) -> None:
        # Re-entrant: `_schedule` prunes a dead subscriber while `publish` holds it.
        self._lock = threading.RLock()
        self._last_seq = 0
        self._buffer: deque[StreamEvent] = deque(maxlen=replay_limit)
        self._subscribers: set[Subscription] = set()

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(
        self,
        event_type: EventType,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        task_id: str | None = None,
    ) -> StreamEvent:
        """Stamp and broadcast one event. Thread-safe; never waits on a subscriber."""
        with self._lock:
            event = StreamEvent(
                seq=self._last_seq + 1,
                ts=_now_iso(),
                task_id=task_id,
                type=event_type,
                payload=dict(payload or {}),
            )
            self._last_seq = event.seq
            self._buffer.append(event)
            # Scheduled under the lock, so callbacks reach each loop in seq order.
            for subscription in list(self._subscribers):
                self._schedule(subscription, event)
        return event

    def subscribe(self, loop: asyncio.AbstractEventLoop, since: int | None) -> Subscription:
        """Register a subscriber whose backlog is the retained events after `since`.

        `since=None` is a fresh subscription: everything still retained is replayed.
        A given `since` must be exact. If an event after it has been evicted, or it is
        ahead of this core, the client's state cannot be brought up to date, so
        `ReplayUnavailableError` is raised instead of replaying a stream with a hole.

        Snapshot and registration share one lock, so no event is missed or delivered
        twice across the boundary.
        """
        with self._lock:
            if since is not None:
                oldest = self._buffer[0].seq if self._buffer else self._last_seq + 1
                if since > self._last_seq or since + 1 < oldest:
                    raise ReplayUnavailableError(
                        f"since={since}, retained={oldest}..{self._last_seq}"
                    )
            subscription = Subscription(
                loop=loop,
                backlog=deque(e for e in self._buffer if since is None or e.seq > since),
            )
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)
        subscription.closed = True

    def close(self) -> None:
        """Tell every subscriber the stream is over. Used on core shutdown."""
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscription in subscribers:
            self._schedule(subscription, Closed(CLOSE_GOING_AWAY))

    def _schedule(self, subscription: Subscription, item: StreamEvent | Closed) -> None:
        try:
            subscription.loop.call_soon_threadsafe(subscription.deliver, item)
        except RuntimeError:
            # The subscriber's loop is closed; it can never read again.
            with self._lock:
                self._subscribers.discard(subscription)
