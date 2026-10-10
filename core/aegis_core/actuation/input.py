"""Synthetic input, and the abort path that guarantees no key survives it.

Two rules from `REMEMBER.md` shape this module:

* **Invariant 1 — the human always wins.** Every sequence checks the
  `PreemptSignal` between individual events, so an action aborts *mid-sequence*
  rather than after it. Waits are `signal.wait(...)`, not `time.sleep(...)`, so
  a preemption ends a pause immediately instead of after it expires.
* **Invariant 3 — no held key survives a stop.** `InputController` tracks every
  key and mouse button it is holding. Every abort path runs `release_all()`,
  which emits a `KEYUP` for each of them, modifiers last. A stuck `Ctrl` is a P0
  bug, so `release_all()` never gives up part-way: one failing `KEYUP` does not
  stop the others, and the failures are raised together afterwards.

`SendInputBackend` is the only thing in the core allowed to call `SendInput`,
and it stamps `AEGIS_SIGNATURE` on every event it builds — see `signature.py`
for why the preemption hook depends on that being exceptionless.

Positions are physical pixels on the virtual desktop (`perception/display.py`,
`P2-01`), and every absolute move re-reads the display layout first and refuses if it
changed since the position was measured: a box from before a monitor was unplugged
is exactly how a click lands somewhere nobody chose. Keys go out with the scan code
Windows maps them to and the extended flag `keys.is_extended()` decides, so an app
that reads scan codes sees the key that was meant. Given a `Motion` (`motion.py`,
`P3-03`), the pointer travels a human-like path to its target and rests there
before a press, so hover states have the moves and the moment they need.

**Behind a higher-integrity window (`P3-20`, `integrity.py`)** Windows drops all
of our input while `SendInput` reports success, and our hooks cannot see the
human either. So every checkpoint also asks the backend whether its events can
land; if not, the action stops with `WalledOffError` and the preemption signal is
set, which pauses the task like a takeover. A release sent in that state is
dropped too, so it is recorded as *owed* and sent again, by a thread of its own,
the moment the foreground stops outranking us.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Protocol

from aegis_core.actuation import win32
from aegis_core.actuation.integrity import Blocker, foreground_blocker
from aegis_core.actuation.keys import (
    MODIFIER_VKS,
    VK_CONTROL,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MENU,
    VK_RCONTROL,
    VK_RETURN,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    VK_SHIFT,
    is_extended,
    parse_combo,
)
from aegis_core.actuation.killswitch import KillSwitch
from aegis_core.actuation.motion import Motion, duration, dwell, plan_path
from aegis_core.actuation.preempt import PreemptEvent, PreemptSignal
from aegis_core.actuation.signature import AEGIS_SIGNATURE
from aegis_core.perception.display import DisplayLayout, OffScreenError, Point, verify_layout

#: Re-exported: older callers import the modifier names from here.
__all__ = [
    "MODIFIER_VKS",
    "VK_CONTROL",
    "VK_LCONTROL",
    "VK_LMENU",
    "VK_LSHIFT",
    "VK_LWIN",
    "VK_MENU",
    "VK_RCONTROL",
    "VK_RETURN",
    "VK_RMENU",
    "VK_RSHIFT",
    "VK_RWIN",
    "VK_SHIFT",
    "InputAbortedError",
    "InputBackend",
    "InputController",
    "MouseButton",
    "ReleaseFailedError",
    "SendInputBackend",
    "WalledOffError",
]

log = logging.getLogger(__name__)

#: A focus change passes through "no window has focus" for an instant; only this
#: long without one counts as a secure desktop or the lock screen.
NO_FOREGROUND_GRACE_S = 0.25
#: How often owed releases are retried while the foreground still outranks us.
OWED_POLL_S = 0.02


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class InputAbortedError(RuntimeError):
    """The human took over, so the in-flight action stopped part-way.

    `released` is what `release_all()` let go of on the way out — the evidence
    that invariant 3 held.
    """

    def __init__(self, released: Sequence[int] = (), buttons: Sequence[MouseButton] = ()) -> None:
        super().__init__("aborted: the user took over")
        self.released: tuple[int, ...] = tuple(released)
        self.released_buttons: tuple[MouseButton, ...] = tuple(buttons)


class WalledOffError(InputAbortedError):
    """A window that outranks us has focus, so nothing we send would land.

    An `InputAbortedError`, because what follows is the same: everything held is
    released (or owed) and the task pauses. `blocker` says which window.
    """

    def __init__(
        self, blocker: Blocker, released: Sequence[int] = (), buttons: Sequence[MouseButton] = ()
    ) -> None:
        super().__init__(released, buttons)
        self.blocker = blocker
        self.args = (
            f"stopped: {blocker.describe()} has focus, and Windows drops input sent to it",
        )


class ReleaseFailedError(RuntimeError):
    """A `KEYUP` did not reach Windows. This is a P0: a key may still be down."""


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class InputBackend(Protocol):
    """The narrow surface `InputController` needs. One real, one fake in tests."""

    def key(self, vk: int, *, down: bool) -> None: ...

    def unicode_char(self, char: str) -> None: ...

    def mouse_move(self, dx: int, dy: int) -> None: ...

    def mouse_move_absolute(self, ax: int, ay: int) -> None:
        """Move to `(ax, ay)` in `SendInput`'s 0..65535 virtual-desktop units."""
        ...

    def mouse_button(self, button: MouseButton, *, down: bool) -> None: ...

    def scroll(self, dx: int, dy: int) -> None: ...

    def blocker(self) -> Blocker | None:
        """What would stop our events landing right now, or `None` if they can."""
        ...

    def key_is_down(self, vk: int) -> bool:
        """Whether Windows has `vk` down. Meaningful only while `blocker()` is `None`."""
        ...

    def button_is_down(self, button: MouseButton) -> bool: ...


class SendInputBackend:
    """The real thing: `SendInput`, with `AEGIS_SIGNATURE` on every event.

    Nothing else in the core may call `SendInput`. Every `INPUT` record is built
    by one of the `_key`/`_mouse` helpers below, and both set `dwExtraInfo`
    unconditionally, so an untagged AEGIS event cannot be written without
    editing this class.
    """

    def __init__(self, signature: int = AEGIS_SIGNATURE) -> None:
        self._signature = signature

    # -- record builders ----------------------------------------------------

    def _key_event(self, *, vk: int, scan: int, flags: int) -> win32.INPUT:
        record = win32.INPUT(type=win32.INPUT_KEYBOARD)
        record.u.ki = win32.KEYBDINPUT(
            wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=self._signature
        )
        return record

    def _mouse_event(self, *, dx: int = 0, dy: int = 0, data: int = 0, flags: int) -> win32.INPUT:
        record = win32.INPUT(type=win32.INPUT_MOUSE)
        record.u.mi = win32.MOUSEINPUT(
            dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=self._signature
        )
        return record

    # -- InputBackend -------------------------------------------------------

    def key(self, vk: int, *, down: bool) -> None:
        mapped = win32.scan_code(vk)
        flags = 0 if down else win32.KEYEVENTF_KEYUP
        if is_extended(vk, mapped):
            flags |= win32.KEYEVENTF_EXTENDEDKEY
        # An `E1` key (Pause) has no one-byte scan code; the VK alone carries it.
        scan = 0 if (mapped >> 8) == 0xE1 else mapped & 0xFF
        win32.send_input([self._key_event(vk=vk, scan=scan, flags=flags)])

    def unicode_char(self, char: str) -> None:
        r"""Type one character. `\n` becomes Enter; astral chars go as a surrogate pair."""
        if char == "\n":
            self.key(VK_RETURN, down=True)
            self.key(VK_RETURN, down=False)
            return

        # KEYEVENTF_UNICODE carries one UTF-16 code unit per event, so anything
        # above the BMP is sent as its two surrogates, in one batch.
        encoded = char.encode("utf-16-le")
        units = [encoded[i] | (encoded[i + 1] << 8) for i in range(0, len(encoded), 2)]
        events = [
            self._key_event(
                vk=0,
                scan=unit,
                flags=win32.KEYEVENTF_UNICODE | (win32.KEYEVENTF_KEYUP if up else 0),
            )
            for unit in units
            for up in (False, True)
        ]
        win32.send_input(events)

    def mouse_move(self, dx: int, dy: int) -> None:
        win32.send_input([self._mouse_event(dx=dx, dy=dy, flags=win32.MOUSEEVENTF_MOVE)])

    def mouse_move_absolute(self, ax: int, ay: int) -> None:
        flags = win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_ABSOLUTE | win32.MOUSEEVENTF_VIRTUALDESK
        win32.send_input([self._mouse_event(dx=ax, dy=ay, flags=flags)])

    def mouse_button(self, button: MouseButton, *, down: bool) -> None:
        flags = {
            (MouseButton.LEFT, True): win32.MOUSEEVENTF_LEFTDOWN,
            (MouseButton.LEFT, False): win32.MOUSEEVENTF_LEFTUP,
            (MouseButton.RIGHT, True): win32.MOUSEEVENTF_RIGHTDOWN,
            (MouseButton.RIGHT, False): win32.MOUSEEVENTF_RIGHTUP,
            (MouseButton.MIDDLE, True): win32.MOUSEEVENTF_MIDDLEDOWN,
            (MouseButton.MIDDLE, False): win32.MOUSEEVENTF_MIDDLEUP,
        }[(button, down)]
        win32.send_input([self._mouse_event(flags=flags)])

    def scroll(self, dx: int, dy: int) -> None:
        events = []
        if dy:
            events.append(
                self._mouse_event(data=dy * win32.WHEEL_DELTA, flags=win32.MOUSEEVENTF_WHEEL)
            )
        if dx:
            events.append(
                self._mouse_event(data=dx * win32.WHEEL_DELTA, flags=win32.MOUSEEVENTF_HWHEEL)
            )
        win32.send_input(events)

    def blocker(self) -> Blocker | None:
        return foreground_blocker()

    def key_is_down(self, vk: int) -> bool:
        return win32.key_is_down(vk)

    def button_is_down(self, button: MouseButton) -> bool:
        """Either physical button that could be carrying `button`.

        `GetAsyncKeyState` reports physical buttons; with the buttons swapped, which
        one our press went to is Windows' business, so both are asked. Saying "down"
        once too often costs one stray button-up; saying "up" wrongly strands a drag.
        """
        vks = {
            MouseButton.LEFT: {win32.VK_LBUTTON},
            MouseButton.RIGHT: {win32.VK_RBUTTON},
            MouseButton.MIDDLE: {win32.VK_MBUTTON},
        }[button]
        if button is not MouseButton.MIDDLE and win32.buttons_swapped():
            vks = {win32.VK_LBUTTON, win32.VK_RBUTTON}
        return any(win32.key_is_down(vk) for vk in vks)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class InputController:
    """Sequences of synthetic events that abort cleanly when the human takes over.

    Thread-safe: the held-key registry is guarded, because `release_all()` is
    also what the kill switch (`P3-06`) calls, from another thread, possibly
    while a sequence is still running.
    """

    def __init__(
        self,
        backend: InputBackend,
        preempt: PreemptSignal | None = None,
        *,
        kill: KillSwitch | None = None,
        inter_event_delay: float = 0.002,
        layout_check: Callable[[DisplayLayout], DisplayLayout] = verify_layout,
        motion: Motion | None = None,
        cursor: Callable[[], Point | None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if inter_event_delay < 0:
            raise ValueError("inter_event_delay must not be negative")
        self._backend = backend
        self._preempt = preempt
        self._delay = inter_event_delay
        self._lock = threading.RLock()
        self._held_keys: list[int] = []
        self._held_buttons: list[MouseButton] = []
        #: Released while walled off, so probably still down: see `_owe()`.
        self._owed_keys: set[int] = set()
        self._owed_buttons: set[MouseButton] = set()
        self._owed_thread: threading.Thread | None = None
        self._kill = kill
        self._layout_check = layout_check
        #: `None` moves in one jump; the agent passes `motion.HUMAN`.
        self._motion = motion
        self._cursor = cursor if cursor is not None else _cursor_point
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 - motion, not secrets
        if kill is not None:
            kill.attach(self)

    # -- state --------------------------------------------------------------

    @property
    def held_keys(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._held_keys)

    @property
    def held_buttons(self) -> tuple[MouseButton, ...]:
        with self._lock:
            return tuple(self._held_buttons)

    @property
    def held_modifiers(self) -> tuple[int, ...]:
        return tuple(vk for vk in self.held_keys if vk in MODIFIER_VKS)

    @property
    def owed(self) -> tuple[frozenset[int], frozenset[MouseButton]]:
        """Keys and buttons whose release Windows may have dropped, not yet re-sent."""
        with self._lock:
            return frozenset(self._owed_keys), frozenset(self._owed_buttons)

    # -- abort plumbing -----------------------------------------------------

    def _checkpoint(self) -> None:
        """Raise if the human took over or the kill switch fired.

        Called before and between every event.
        """
        if self._preempt is not None and self._preempt.is_set():
            raise InputAbortedError()
        if self._kill is not None and self._kill.engaged:
            raise InputAbortedError()
        self._check_wall()

    def _check_wall(self) -> None:
        """Raise `WalledOffError`, and pause the task, if our events cannot land."""
        blocker = self._backend.blocker()
        deadline = time.monotonic() + NO_FOREGROUND_GRACE_S
        while blocker is not None and blocker.no_foreground and time.monotonic() < deadline:
            time.sleep(0.01)
            if self._preempt is not None and self._preempt.is_set():
                raise InputAbortedError()
            blocker = self._backend.blocker()
        if blocker is None:
            return
        log.warning(
            "input.walled_off",
            extra={"image": blocker.image, "pid": blocker.pid, "integrity": blocker.integrity},
        )
        if self._preempt is not None:
            # Behind this window the hooks cannot see the human either, so carrying
            # on after it closes would be acting blind. Pause, like a takeover.
            self._preempt.trigger(PreemptEvent("walled_off", 0, time.perf_counter()))
        raise WalledOffError(blocker)

    def _pace(self) -> None:
        """Wait between events, but wake instantly on preemption."""
        if self._preempt is not None:
            self._preempt.wait(self._delay)
        elif self._delay:
            # No signal to wait on, so nothing to wake up for.
            time.sleep(self._delay)
        self._checkpoint()

    def _wait(self, seconds: float) -> None:
        """Wait `seconds`, waking at once on preemption, then checkpoint."""
        if seconds > 0:
            if self._preempt is not None:
                self._preempt.wait(seconds)
            else:
                time.sleep(seconds)
        self._checkpoint()

    @contextmanager
    def _action(self) -> Iterator[None]:
        """Wrap a sequence so *any* early exit releases what we are holding.

        Normal completion does not release: `key_down()` exists precisely so a
        caller can hold a modifier across calls.
        """
        try:
            # Inside the try: if the signal is already set, keys held from an
            # earlier call must still be released before we unwind.
            self._checkpoint()
            # Settle any release still owed before this sequence presses anything,
            # so the retry thread cannot let go of a key this sequence holds.
            self.settle_owed()
            yield
            # The kill switch sweeps from another thread, so an event sent in
            # the gap between its sweep and this sequence's next checkpoint
            # would otherwise outlive it — `key_down()` has no next checkpoint.
            if self._kill is not None and self._kill.engaged:
                raise InputAbortedError()
            # Likewise a window that came forward after the last checkpoint: the
            # final event may have been dropped, so the action did not happen.
            self._check_wall()
        except BaseException as exc:
            released, buttons = self._release_all_unchecked()
            if isinstance(exc, InputAbortedError):
                exc.released = released
                exc.released_buttons = buttons
            raise

    # -- release ------------------------------------------------------------

    def release_all(self) -> tuple[tuple[int, ...], tuple[MouseButton, ...]]:
        """Emit a `KEYUP` for every key and button AEGIS is holding.

        Safe from any thread and from an exception path. It deliberately does
        *not* checkpoint: a preemption in progress is the reason it was called,
        and must not stop it half-way.

        Raises `ReleaseFailedError` only after attempting every single release.
        """
        return self._release_all_unchecked()

    def _release_all_unchecked(self) -> tuple[tuple[int, ...], tuple[MouseButton, ...]]:
        failures: list[BaseException] = []
        released: list[int] = []
        released_buttons: list[MouseButton] = []

        with self._lock:
            for button in reversed(self._held_buttons):
                try:
                    self._backend.mouse_button(button, down=False)
                except Exception as exc:  # every button still gets its turn
                    failures.append(exc)
                else:
                    released_buttons.append(button)
            self._held_buttons.clear()

            # Ordinary keys first, then modifiers — releasing Ctrl before the
            # key it modifies makes an application see a bare keypress.
            ordered = [vk for vk in reversed(self._held_keys) if vk not in MODIFIER_VKS]
            ordered += [vk for vk in reversed(self._held_keys) if vk in MODIFIER_VKS]
            for vk in ordered:
                try:
                    self._backend.key(vk, down=False)
                except Exception as exc:  # a stuck modifier is a P0: keep going
                    failures.append(exc)
                else:
                    released.append(vk)
            self._held_keys.clear()

            if released or released_buttons:
                blocker = self._backend.blocker()
                if blocker is not None:
                    self._owe(blocker, released, released_buttons)

        if failures:
            raise ReleaseFailedError(
                f"{len(failures)} key/button release(s) failed; "
                f"released {released!r} and {released_buttons!r}"
            ) from failures[0]
        return tuple(released), tuple(released_buttons)

    # -- owed releases ------------------------------------------------------

    def _owe(self, blocker: Blocker, keys: Sequence[int], buttons: Sequence[MouseButton]) -> None:
        """Record releases Windows probably dropped, and make sure something retries them.

        Called with `_lock` held. Measured: a `KEYUP` sent while a window that
        outranks us has focus is dropped, the key stays down, and `GetAsyncKeyState`
        cannot show it until that window loses focus. A stuck Ctrl would then turn
        the human's typing in that admin window into shortcuts (invariant 3).
        """
        self._owed_keys.update(keys)
        self._owed_buttons.update(buttons)
        log.warning(
            "input.release_owed",
            extra={"keys": len(keys), "buttons": len(buttons), "image": blocker.image},
        )
        if self._owed_thread is None or not self._owed_thread.is_alive():
            self._owed_thread = threading.Thread(
                target=self._retry_owed, name="aegis-owed-release", daemon=True
            )
            self._owed_thread.start()

    def _retry_owed(self) -> None:
        while not self.settle_owed():
            time.sleep(OWED_POLL_S)

    def settle_owed(self) -> bool:
        """Re-send owed releases if our events can land now. True when nothing is owed.

        Only what Windows still reports down is released, and nothing this
        controller is holding again. Like `release_all()`, it never checkpoints.
        """
        with self._lock:
            if not (self._owed_keys or self._owed_buttons):
                return True
            if self._backend.blocker() is not None:
                return False
            pending = [vk for vk in self._owed_keys if vk not in self._held_keys]
            # Ordinary keys before modifiers, as in `release_all()`.
            keys = [vk for vk in pending if vk not in MODIFIER_VKS]
            keys += [vk for vk in pending if vk in MODIFIER_VKS]
            buttons = [b for b in self._owed_buttons if b not in self._held_buttons]
            sent_keys: list[int] = []
            sent_buttons: list[MouseButton] = []
            try:
                for button in buttons:
                    if self._backend.button_is_down(button):
                        self._backend.mouse_button(button, down=False)
                    sent_buttons.append(button)
                for vk in keys:
                    if self._backend.key_is_down(vk):
                        self._backend.key(vk, down=False)
                    sent_keys.append(vk)
            except Exception:  # retried next round, like any dropped release
                log.exception("input.owed_release_failed")
            # Focus may have moved again while we sent: then this round proves nothing.
            if self._backend.blocker() is not None:
                return False
            self._owed_keys.difference_update(sent_keys)
            self._owed_buttons.difference_update(sent_buttons)
            if sent_keys or sent_buttons:
                log.info(
                    "input.owed_released",
                    extra={"keys": len(sent_keys), "buttons": len(sent_buttons)},
                )
            # A key this controller holds again is a hold now, not a debt.
            self._owed_keys.difference_update(self._held_keys)
            self._owed_buttons.difference_update(self._held_buttons)
            return not (self._owed_keys or self._owed_buttons)

    # -- primitives ---------------------------------------------------------

    def key_down(self, vk: int) -> None:
        with self._action():
            self._backend.key(vk, down=True)
            with self._lock:
                self._held_keys.append(vk)

    def key_up(self, vk: int) -> None:
        with self._action():
            self._backend.key(vk, down=False)
            with self._lock:
                if vk in self._held_keys:
                    self._held_keys.remove(vk)

    def tap(self, vk: int) -> None:
        with self._action():
            self._backend.key(vk, down=True)
            with self._lock:
                self._held_keys.append(vk)
            self._pace()
            self._backend.key(vk, down=False)
            with self._lock:
                self._held_keys.remove(vk)

    def press_combo(self, *vks: int) -> None:
        """Hold `vks` in order, then release them in reverse. Ctrl+Shift+N and friends."""
        if not vks:
            raise ValueError("press_combo needs at least one key")
        with self._action():
            for vk in vks:
                self._backend.key(vk, down=True)
                with self._lock:
                    self._held_keys.append(vk)
                self._pace()
            for vk in reversed(vks):
                self._backend.key(vk, down=False)
                with self._lock:
                    self._held_keys.remove(vk)
                self._pace()

    def press_keys(self, spec: str) -> None:
        """`press_combo()` from text: `"ctrl+shift+n"`, `"alt+f4"`, `"enter"`."""
        self.press_combo(*parse_combo(spec))

    def type_text(self, text: str) -> None:
        with self._action():
            for char in text:
                self._backend.unicode_char(char)
                self._pace()

    def move_by(self, dx: int, dy: int, *, steps: int = 1) -> None:
        """Relative motion, in mickeys. Pointer acceleration applies: use `move_to()`."""
        if steps < 1:
            raise ValueError("steps must be at least 1")
        with self._action():
            for index in range(steps):
                step_x = dx * (index + 1) // steps - dx * index // steps
                step_y = dy * (index + 1) // steps - dy * index // steps
                self._backend.mouse_move(step_x, step_y)
                self._pace()

    def _absolute(self, layout: DisplayLayout, *points: Point) -> list[tuple[int, int]]:
        """Every point in `SendInput` units, under a layout re-read **now**.

        All of them are converted before anything is sent, so a path that crosses a
        dead zone between monitors is refused whole rather than half-performed.
        """
        current = self._layout_check(layout)
        return [current.to_absolute(point) for point in points]

    def _travel(
        self, layout: DisplayLayout, end: Point, *, start: Point | None = None, strict: bool = False
    ) -> tuple[list[tuple[int, int]], float]:
        """The moves that take the pointer to `end`, and the pause after each one.

        With no `Motion`, or no known start, it is one jump. Otherwise a planned curve;
        if the curve bows off every monitor, a straight line at the same pace; and if
        even that crosses a dead zone, a jump — unless `strict` (a drag, where the
        button is down the whole way), which refuses instead.
        """
        origin = start if start is not None else self._cursor()
        if self._motion is None or origin is None:
            return self._absolute(layout, end), 0.0
        for motion in (self._motion, self._motion.straight()):
            path = plan_path(origin, end, motion, self._rng)
            try:
                absolute = self._absolute(layout, *path)
            except OffScreenError:
                continue
            total = duration(abs(end.x - origin.x) + abs(end.y - origin.y), motion)
            return absolute, total / len(absolute)
        if strict:
            raise OffScreenError("The path between these points leaves every monitor.")
        return self._absolute(layout, end), 0.0

    def _run(self, moves: list[tuple[int, int]], pause: float) -> None:
        for index, (ax, ay) in enumerate(moves):
            self._backend.mouse_move_absolute(ax, ay)
            if index < len(moves) - 1:
                self._wait(pause)

    def _settle(self) -> None:
        """The rest before a press: a human dwell with `Motion`, one pace without."""
        if self._motion is None:
            self._pace()
        else:
            self._wait(dwell(self._motion, self._rng))

    def move_to(self, point: Point, layout: DisplayLayout) -> None:
        """Put the cursor on `point`, a physical pixel measured under `layout`.

        Raises `LayoutChangedError` if the monitors changed since `layout` was read,
        and `OffScreenError` if `point` is on no monitor. Nothing moves in either case.
        """
        with self._action():
            self._run(*self._travel(layout, point))

    def click_at(
        self,
        point: Point,
        layout: DisplayLayout,
        *,
        button: MouseButton = MouseButton.LEFT,
        count: int = 1,
    ) -> None:
        """Move to `point`, rest, then click: single, double (`count=2`) or right."""
        if count < 1:
            raise ValueError("count must be at least 1")
        with self._action():
            self._run(*self._travel(layout, point))
            self._settle()
            self.click(button, count=count)

    def drag_to(
        self,
        start: Point,
        end: Point,
        layout: DisplayLayout,
        *,
        button: MouseButton = MouseButton.LEFT,
        steps: int = 8,
    ) -> None:
        """Move to `start`, press, travel to `end`, release.

        Both legs are planned and checked before anything moves, so a drag that would
        cross the gap between two monitors never starts. Without a `Motion` the drag
        is `steps` straight segments.
        """
        if steps < 1:
            raise ValueError("steps must be at least 1")
        with self._action():
            approach, approach_pause = self._travel(layout, start)
            if self._motion is None:
                straight = [
                    Point(
                        start.x + (end.x - start.x) * index // steps,
                        start.y + (end.y - start.y) * index // steps,
                    )
                    for index in range(1, steps + 1)
                ]
                drag, drag_pause = self._absolute(layout, *straight), self._delay
            else:
                drag, drag_pause = self._travel(layout, end, start=start, strict=True)
            self._run(approach, approach_pause)
            self._settle()
            self._backend.mouse_button(button, down=True)
            with self._lock:
                self._held_buttons.append(button)
            self._settle()
            self._run(drag, drag_pause)
            self._pace()
            self._backend.mouse_button(button, down=False)
            with self._lock:
                self._held_buttons.remove(button)

    def button_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        with self._action():
            self._backend.mouse_button(button, down=True)
            with self._lock:
                self._held_buttons.append(button)

    def button_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        with self._action():
            self._backend.mouse_button(button, down=False)
            with self._lock:
                if button in self._held_buttons:
                    self._held_buttons.remove(button)

    def click(self, button: MouseButton = MouseButton.LEFT, *, count: int = 1) -> None:
        if count < 1:
            raise ValueError("count must be at least 1")
        with self._action():
            for _ in range(count):
                self._backend.mouse_button(button, down=True)
                with self._lock:
                    self._held_buttons.append(button)
                self._pace()
                self._backend.mouse_button(button, down=False)
                with self._lock:
                    self._held_buttons.remove(button)
                self._pace()

    def drag(
        self,
        dx: int,
        dy: int,
        *,
        button: MouseButton = MouseButton.LEFT,
        steps: int = 8,
    ) -> None:
        """Press, move, release. Aborting part-way still releases the button."""
        with self._action():
            self._backend.mouse_button(button, down=True)
            with self._lock:
                self._held_buttons.append(button)
            self._pace()
            for index in range(steps):
                step_x = dx * (index + 1) // steps - dx * index // steps
                step_y = dy * (index + 1) // steps - dy * index // steps
                self._backend.mouse_move(step_x, step_y)
                self._pace()
            self._backend.mouse_button(button, down=False)
            with self._lock:
                self._held_buttons.remove(button)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        with self._action():
            self._backend.scroll(dx, dy)


def _cursor_point() -> Point | None:
    where = win32.cursor_pos()
    return None if where is None else Point(*where)
