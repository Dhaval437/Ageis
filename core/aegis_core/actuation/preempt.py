"""Preemption: the human always wins (`REMEMBER.md` invariant 1).

`InputMonitor` runs a dedicated thread that installs `WH_MOUSE_LL` and
`WH_KEYBOARD_LL` and pumps a message loop. The hook callbacks compare
`dwExtraInfo` against `AEGIS_SIGNATURE`; anything that is not ours is the human,
and sets `PreemptSignal` — nothing else. The in-flight action aborts because it
polls that signal (`input.py`), not because the hook reaches into it.

**The hook thread does no I/O.** No logging, no sockets, no database, no
blocking. A low-level hook callback runs on Windows' input path: if it takes
longer than `LowLevelHooksTimeout` (300 ms by default) Windows silently
uninstalls the hook, and until then every keystroke on the machine waits behind
it. Reacting to a preemption — pausing the task, emitting `preempt.triggered` —
happens on the watcher thread that `PreemptSignal.watch()` starts.

Windows-only, like the rest of AEGIS (`REMEMBER.md § 4`).
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from aegis_core.actuation import win32
from aegis_core.actuation.signature import AEGIS_SIGNATURE

PreemptSource = Literal["mouse", "keyboard"]

#: How long `start()` waits for the hook thread to install its hooks.
_START_TIMEOUT_S = 5.0
#: How long `stop()` waits for the message loop to drain.
_STOP_TIMEOUT_S = 5.0
#: How often the watcher thread checks the signal while idle.
_WATCH_POLL_S = 0.02


@dataclass(frozen=True, slots=True)
class PreemptEvent:
    """What the hook saw. `at` is `time.perf_counter()` read inside the callback."""

    source: PreemptSource
    message: int
    at: float


class PreemptSignal:
    """A thread-safe "the human took over" flag.

    `trigger()` is called from the hook thread and must stay O(1): two attribute
    writes and an `Event.set()`. Everything expensive is a listener, dispatched
    from the watcher thread.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._last: PreemptEvent | None = None
        self._listeners: list[Callable[[PreemptEvent], None]] = []
        self._listener_lock = threading.Lock()

    # -- hook thread --------------------------------------------------------

    def trigger(self, event: PreemptEvent) -> None:
        """Record a preemption. Fast enough to call from a hook callback.

        `_last` is written *before* the event is set, so anything that observes
        `is_set()` also observes `last`.
        """
        self._last = event
        self._event.set()

    # -- everyone else ------------------------------------------------------

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def last(self) -> PreemptEvent | None:
        return self._last

    def wait(self, timeout: float | None = None) -> bool:
        """Block until preempted. Doubles as the abort-aware sleep in `input.py`."""
        return self._event.wait(timeout)

    def clear(self) -> None:
        """Re-arm. Only the resume path may call this — never an action itself."""
        self._event.clear()
        self._last = None

    def add_listener(self, listener: Callable[[PreemptEvent], None]) -> None:
        """Register a reaction (pause the task, emit `preempt.triggered`, …).

        Listeners run on the watcher thread started by `watch()`, never on the
        hook thread. They fire once per preemption.
        """
        with self._listener_lock:
            self._listeners.append(listener)

    @contextmanager
    def watch(self) -> Iterator[None]:
        """Run the registered listeners on their own thread when preemption fires."""
        stop = threading.Event()

        def pump() -> None:
            while not stop.is_set():
                if not self._event.wait(_WATCH_POLL_S):
                    continue
                event = self._last
                if event is None:  # cleared between the wait and the read
                    continue
                with self._listener_lock:
                    listeners = tuple(self._listeners)
                for listener in listeners:
                    listener(event)
                return

        thread = threading.Thread(target=pump, name="aegis-preempt-watch", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=_STOP_TIMEOUT_S)


class InputMonitor:
    """Installs the low-level hooks on a dedicated thread that does no I/O.

    Usage::

        signal = PreemptSignal()
        with InputMonitor(signal):
            ...  # signal is set by any input AEGIS did not synthesise
    """

    def __init__(
        self,
        signal: PreemptSignal,
        *,
        watch_mouse: bool = True,
        watch_keyboard: bool = True,
        mouse_move_threshold_px: int = 0,
        signature: int = AEGIS_SIGNATURE,
    ) -> None:
        if not (watch_mouse or watch_keyboard):
            raise ValueError("InputMonitor must watch at least one input device")
        if mouse_move_threshold_px < 0:
            raise ValueError("mouse_move_threshold_px must not be negative")

        self._signal = signal
        self._watch_mouse = watch_mouse
        self._watch_keyboard = watch_keyboard
        self._move_threshold = mouse_move_threshold_px
        self._signature = signature

        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._install_error: BaseException | None = None
        self._callback_errors = 0
        self._last_point: tuple[int, int] | None = None
        # A ctypes callback is kept alive only by our reference to it. If one is
        # garbage collected while Windows still holds the hook, the next
        # keystroke on the machine calls freed memory.
        self._procs: list[Callable[..., int]] = []
        self._hooks: list[int] = []

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def callback_errors(self) -> int:
        """Exceptions swallowed inside a hook callback. Should always be 0."""
        return self._callback_errors

    def start(self) -> None:
        """Install the hooks and return once they are live."""
        if self.running:
            raise RuntimeError("InputMonitor is already running")

        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="aegis-input-hooks", daemon=True)
        self._thread.start()

        if not self._ready.wait(_START_TIMEOUT_S):
            self._thread = None
            raise TimeoutError("input hook thread did not install its hooks in time")
        if self._install_error is not None:
            self._thread = None
            raise self._install_error

    def stop(self) -> None:
        """Uninstall the hooks and join the thread. Idempotent."""
        thread, thread_id = self._thread, self._thread_id
        self._thread = None
        if thread is None:
            return

        if thread_id is not None:
            win32.post_quit(thread_id)
        thread.join(timeout=_STOP_TIMEOUT_S)
        if thread.is_alive():  # pragma: no cover - a wedged message loop
            raise TimeoutError("input hook thread did not stop")
        self._thread_id = None

    def __enter__(self) -> InputMonitor:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -- hook thread --------------------------------------------------------

    def _run(self) -> None:
        self._install_error = None
        try:
            self._thread_id = win32.current_thread_id()
            if self._watch_keyboard:
                self._install(win32.WH_KEYBOARD_LL, self._on_keyboard)
            if self._watch_mouse:
                self._install(win32.WH_MOUSE_LL, self._on_mouse)
        except BaseException as exc:  # re-raised on the caller's thread by start()
            self._install_error = exc
            self._uninstall()
        finally:
            self._ready.set()

        if self._install_error is not None:
            return

        try:
            self._pump()
        finally:
            self._uninstall()

    def _install(self, hook_id: int, handler: Callable[[int, int, int], int]) -> None:
        proc = win32.HOOKPROC(handler)
        handle = win32.user32.SetWindowsHookExW(hook_id, proc, None, 0)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._procs.append(proc)
        self._hooks.append(handle)

    def _uninstall(self) -> None:
        while self._hooks:
            win32.user32.UnhookWindowsHookEx(self._hooks.pop())
        self._procs.clear()

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while True:
            # Low-level hook callbacks are delivered by GetMessage itself; a
            # thread with no windows has nothing else to dispatch.
            result = win32.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):  # WM_QUIT, or an error we cannot recover from
                return

    # -- callbacks: no I/O, no blocking, no locks ---------------------------

    def _on_keyboard(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == win32.HC_ACTION:
            try:
                info = win32.KBDLLHOOKSTRUCT.from_address(l_param)
                if info.dwExtraInfo != self._signature:
                    self._signal.trigger(PreemptEvent("keyboard", w_param, time.perf_counter()))
            except Exception:  # see the class docstring
                # Raising through a ctypes callback into Windows' input path is
                # undefined behaviour, and logging here would be I/O. Count it
                # instead; the tests assert this stays 0.
                self._callback_errors += 1
        return int(win32.user32.CallNextHookEx(None, n_code, w_param, l_param))

    def _on_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == win32.HC_ACTION:
            try:
                info = win32.MSLLHOOKSTRUCT.from_address(l_param)
                point = (info.pt.x, info.pt.y)
                previous, self._last_point = self._last_point, point
                if info.dwExtraInfo != self._signature and not self._is_jitter(
                    w_param, previous, point
                ):
                    self._signal.trigger(PreemptEvent("mouse", w_param, time.perf_counter()))
            except Exception:  # see the class docstring
                self._callback_errors += 1
        return int(win32.user32.CallNextHookEx(None, n_code, w_param, l_param))

    def _is_jitter(
        self,
        message: int,
        previous: tuple[int, int] | None,
        point: tuple[int, int],
    ) -> bool:
        """True only for a mouse *move* smaller than the configured deadzone.

        The default threshold is 0, so every event counts — that is the
        invariant. The knob exists for machines whose driver emits jitter while
        the mouse sits still; a click or a scroll is never filtered.
        """
        if self._move_threshold == 0 or message != win32.WM_MOUSEMOVE or previous is None:
            return False
        return abs(point[0] - previous[0]) + abs(point[1] - previous[1]) <= self._move_threshold
