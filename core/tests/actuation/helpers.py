"""Shared test doubles and Win32 helpers for the actuation tests."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from aegis_core.actuation.input import MouseButton
from aegis_core.actuation.preempt import PreemptSignal

#: F24. Chosen for the "physical input" injections because no shell, editor, or
#: shortcut in a default Windows install binds it — the tests must not type into
#: whatever window happens to be focused on the developer's machine.
VK_F24 = 0x87


@dataclass
class Recorded:
    """One event the fake backend was asked to send."""

    kind: str
    detail: object
    down: bool | None = None


class RecordingBackend:
    """An `InputBackend` that records instead of touching the desktop.

    `delay` makes a sequence take real time so an abort has something to
    interrupt. `fail_keys` makes a `KEYUP` raise, which is how the release path
    is tested for the "one failure must not strand the other seven" case.
    """

    def __init__(self, *, delay: float = 0.0, fail_keys: frozenset[int] = frozenset()) -> None:
        self.events: list[Recorded] = []
        self.delay = delay
        self.fail_keys = fail_keys
        self._lock = threading.Lock()

    def _record(self, event: Recorded) -> None:
        with self._lock:
            self.events.append(event)
        if self.delay:
            time.sleep(self.delay)

    # -- InputBackend -------------------------------------------------------

    def key(self, vk: int, *, down: bool) -> None:
        if not down and vk in self.fail_keys:
            raise OSError(f"SendInput refused KEYUP for {vk:#04x}")
        self._record(Recorded("key", vk, down))

    def unicode_char(self, char: str) -> None:
        self._record(Recorded("char", char))

    def mouse_move(self, dx: int, dy: int) -> None:
        self._record(Recorded("move", (dx, dy)))

    def mouse_move_absolute(self, ax: int, ay: int) -> None:
        self._record(Recorded("move_to", (ax, ay)))

    def mouse_button(self, button: MouseButton, *, down: bool) -> None:
        self._record(Recorded("button", button, down))

    def scroll(self, dx: int, dy: int) -> None:
        self._record(Recorded("scroll", (dx, dy)))

    # -- assertions ---------------------------------------------------------

    def key_ups(self) -> list[int]:
        with self._lock:
            return [e.detail for e in self.events if e.kind == "key" and e.down is False]  # type: ignore[misc]

    def key_downs(self) -> list[int]:
        with self._lock:
            return [e.detail for e in self.events if e.kind == "key" and e.down is True]  # type: ignore[misc]

    def kinds(self) -> list[str]:
        with self._lock:
            return [e.kind for e in self.events]


# ---------------------------------------------------------------------------
# Real Win32 injection — used only by the hook tests
# ---------------------------------------------------------------------------


def inject_untagged_key(vk: int = VK_F24) -> None:
    """Send a keystroke with `dwExtraInfo = 0` — indistinguishable from a real one.

    A low-level hook cannot tell this from a keypress on the physical keyboard:
    `LLKHF_INJECTED` is deliberately *not* what AEGIS keys off (see
    `signature.py`), so from the hook's point of view this is the human. That is
    what makes it a valid stand-in for a finger on a key in an automated test.
    """
    if sys.platform != "win32":  # pragma: no cover - guarded by the module skip
        raise RuntimeError("Windows only")
    from aegis_core.actuation import win32

    events = []
    for up in (False, True):
        record = win32.INPUT(type=win32.INPUT_KEYBOARD)
        record.u.ki = win32.KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=win32.KEYEVENTF_KEYUP if up else 0,
            time=0,
            dwExtraInfo=0,
        )
        events.append(record)
    win32.send_input(events)


def wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.001
) -> bool:
    """Poll `predicate` until true or `timeout` elapses."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def settle(signal: PreemptSignal, quiet_for: float = 0.15, timeout: float = 5.0) -> bool:
    """Wait for a stretch with no preemption at all, then re-arm the signal.

    The hook watches the *whole machine*, so a developer nudging the mouse while
    the suite runs looks exactly like the thing under test. Rather than sleep and
    hope, wait for a genuinely idle window; the caller skips loudly if there
    never is one.
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        signal.clear()
        if not signal.wait(quiet_for):
            return True
    return False
