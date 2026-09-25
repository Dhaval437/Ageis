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
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Protocol

from aegis_core.actuation import win32
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
from aegis_core.actuation.preempt import PreemptSignal
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
]


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

    # -- abort plumbing -----------------------------------------------------

    def _checkpoint(self) -> None:
        """Raise if the human took over or the kill switch fired.

        Called before and between every event.
        """
        if self._preempt is not None and self._preempt.is_set():
            raise InputAbortedError()
        if self._kill is not None and self._kill.engaged:
            raise InputAbortedError()

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
            yield
            # The kill switch sweeps from another thread, so an event sent in
            # the gap between its sweep and this sequence's next checkpoint
            # would otherwise outlive it — `key_down()` has no next checkpoint.
            if self._kill is not None and self._kill.engaged:
                raise InputAbortedError()
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

        if failures:
            raise ReleaseFailedError(
                f"{len(failures)} key/button release(s) failed; "
                f"released {released!r} and {released_buttons!r}"
            ) from failures[0]
        return tuple(released), tuple(released_buttons)

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
