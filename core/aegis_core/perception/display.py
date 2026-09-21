"""The one coordinate space every screen position in the core is expressed in.

**Physical pixels on the Windows virtual desktop.** The primary monitor's top-left
is `(0, 0)`; a monitor to its left or above it has negative coordinates; the
rectangle that bounds every monitor can contain dead zones that are on no screen
at all. A UIA bounding rectangle, a capture region, a cursor position and a click
target are all points in this one space, so nothing downstream ever has to ask
which space a number is in. There are **no logical (DIP) coordinates in the core.**

That is only true if the process is *per-monitor* DPI aware. A DPI-unaware
`python.exe` — which is what Windows gives us by default — is handed a
virtualised desktop: on a 3200x2000 panel at 200 % it is told the screen is
1600x1000, and every UIA rectangle is halved to match, while `SendInput`'s
absolute coordinates still cover the real panel. Nothing errors; every click just
lands somewhere else. So:

* `ensure_dpi_awareness()` is called by `__main__` before anything else can create
  a window or set the awareness first (a library that sets *system* awareness
  would make the choice permanent).
* `query_layout()` **refuses** to describe the screen to a thread that is not
  per-monitor aware, rather than handing it numbers in the wrong space.

A `DisplayLayout` is a snapshot. Monitors are plugged in, unplugged, rearranged
and rescaled while a task runs, and a bounding box from before the change points
at the wrong thing after it (`RECOVERY.md § 3.3`, display configuration change).
`verify_layout()` is how a caller about to act on an old position proves the
screen it was measured on still exists.
"""

from __future__ import annotations

import hashlib
import logging
from ctypes import wintypes
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from aegis_core.perception import win32

log = logging.getLogger(__name__)

#: Windows' baseline: 96 DPI is 100 % scaling.
BASE_DPI: Final = 96

#: Effective DPI outside this range is not a scaling Windows offers (100 % to 500 %),
#: so a reading outside it is a broken read, not a monitor.
MIN_DPI: Final = 96
MAX_DPI: Final = 480

#: `REVIEW.md § 2`: no unbounded list. Windows has no hard limit, but a desk with
#: more than this is not a desk the product is for, and failing is better than
#: silently leaving a monitor out of the model.
MAX_MONITORS: Final = 16

#: `SendInput`'s `MOUSEEVENTF_ABSOLUTE` coordinates run from 0 to 65 535 across
#: the virtual desktop when `MOUSEEVENTF_VIRTUALDESK` is set.
ABSOLUTE_SPAN: Final = 65_536

#: Monitors and the virtual-screen metrics are separate reads, so a display change
#: can land between them. A mismatch is retried this many times before it is an
#: error; a real change settles within one retry.
QUERY_ATTEMPTS: Final = 3


class DisplayError(RuntimeError):
    """The screen could not be described, or not in a form that can be trusted."""


class DpiAwarenessError(DisplayError):
    """The calling thread would be given virtualised (logical) coordinates."""


class OffScreenError(DisplayError):
    """A point that is on no monitor — outside the desktop, or in a dead zone."""


class LayoutChangedError(DisplayError):
    """The monitors changed since a position was measured; the position is void."""


class DpiAwareness(IntEnum):
    """`DPI_AWARENESS`. Only `PER_MONITOR` yields physical coordinates on every monitor."""

    INVALID = win32.DPI_AWARENESS_INVALID
    UNAWARE = win32.DPI_AWARENESS_UNAWARE
    SYSTEM = win32.DPI_AWARENESS_SYSTEM_AWARE
    PER_MONITOR = win32.DPI_AWARENESS_PER_MONITOR_AWARE


@dataclass(frozen=True, slots=True)
class Point:
    """A physical pixel on the virtual desktop."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class Rect:
    """A physical-pixel rectangle, Win32 style: `right` and `bottom` are **exclusive**.

    A 1920-wide monitor at the origin is `Rect(0, 0, 1920, 1080)`, and pixel 1920
    belongs to whatever is to its right.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, point: Point) -> bool:
        return self.left <= point.x < self.right and self.top <= point.y < self.bottom

    def contains_rect(self, other: Rect) -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


@dataclass(frozen=True, slots=True)
class Monitor:
    """One display, as the virtual desktop places it.

    `device` is the GDI device name (`\\\\.\\DISPLAY1`). It is stable while the
    monitor stays connected, which an `HMONITOR` is not guaranteed to be, so no
    handle is kept.
    """

    device: str
    bounds: Rect
    work_area: Rect
    dpi: int
    primary: bool

    @property
    def scale(self) -> float:
        """The user's scaling setting for this monitor: 1.5 at 150 %."""
        return self.dpi / BASE_DPI


@dataclass(frozen=True)
class DisplayLayout:
    """Every monitor and the virtual desktop that bounds them, at one moment.

    Construction validates the whole snapshot, so a `DisplayLayout` that exists
    is one whose numbers agree with each other. Two layouts are equal exactly
    when every monitor's device, bounds, work area, DPI and primary flag agree.
    """

    monitors: tuple[Monitor, ...]
    virtual: Rect

    def __post_init__(self) -> None:
        if not self.monitors:
            raise DisplayError("Windows reported no monitors.")
        if len(self.monitors) > MAX_MONITORS:
            raise DisplayError(f"More than {MAX_MONITORS} monitors are connected.")
        primaries = [monitor for monitor in self.monitors if monitor.primary]
        if len(primaries) != 1:
            raise DisplayError(f"Expected one primary monitor, found {len(primaries)}.")
        if (primaries[0].bounds.left, primaries[0].bounds.top) != (0, 0):
            raise DisplayError("The primary monitor is not at the virtual-desktop origin.")
        for monitor in self.monitors:
            if monitor.bounds.is_empty:
                raise DisplayError(f"{monitor.device} has an empty area.")
            if not monitor.bounds.contains_rect(monitor.work_area) or monitor.work_area.is_empty:
                raise DisplayError(f"{monitor.device} has a work area outside its bounds.")
            if not MIN_DPI <= monitor.dpi <= MAX_DPI:
                raise DisplayError(f"{monitor.device} reported {monitor.dpi} DPI.")
        # SM_*VIRTUALSCREEN is documented as the bounding rectangle of all monitors.
        # When it is not, the two reads straddled a display change.
        bounding = Rect(
            min(monitor.bounds.left for monitor in self.monitors),
            min(monitor.bounds.top for monitor in self.monitors),
            max(monitor.bounds.right for monitor in self.monitors),
            max(monitor.bounds.bottom for monitor in self.monitors),
        )
        if bounding != self.virtual:
            raise DisplayError("The virtual desktop does not match its monitors.")

    @property
    def primary(self) -> Monitor:
        return next(monitor for monitor in self.monitors if monitor.primary)

    @property
    def fingerprint(self) -> str:
        """A short, stable token for this exact layout, for stamping observations.

        Includes DPI: a scaling change leaves a monitor's physical bounds alone but
        re-lays-out every window on it, so it invalidates positions just as surely.
        """
        canonical = "|".join(
            f"{m.device},{m.bounds},{m.work_area},{m.dpi},{m.primary}" for m in self.monitors
        )
        return hashlib.sha256(f"{canonical}|{self.virtual}".encode()).hexdigest()[:16]

    def monitor_at(self, point: Point) -> Monitor | None:
        """The monitor showing `point`, or `None` if no monitor does."""
        return next((m for m in self.monitors if m.bounds.contains(point)), None)

    def to_absolute(self, point: Point) -> tuple[int, int]:
        """`point` in `SendInput`'s `MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK` units.

        Windows maps an absolute coordinate `n` back to the pixel
        `floor(n * width / 65536)`. The value chosen here is the **middle** of the
        run of `n` that lands on the pixel, not its edge, so the pixel is hit
        exactly for any desktop up to 32 768 px on a side — the whole range of
        Windows' coordinates — and a rounding difference in the other direction
        still cannot move it by one. A point in a dead zone is refused: the cursor
        would be clamped onto some monitor edge that nothing asked for.
        """
        if self.monitor_at(point) is None:
            raise OffScreenError(f"({point.x}, {point.y}) is not on any monitor.")
        return (
            _absolute(point.x - self.virtual.left, self.virtual.width),
            _absolute(point.y - self.virtual.top, self.virtual.height),
        )


def _absolute(offset: int, span: int) -> int:
    return ((2 * offset + 1) * ABSOLUTE_SPAN) // (2 * span)


def current_dpi_awareness() -> DpiAwareness:
    """The calling thread's awareness. It is per-thread: a thread can override the process."""
    value = win32.thread_dpi_awareness()
    try:
        return DpiAwareness(value)
    except ValueError:
        return DpiAwareness.INVALID


def ensure_dpi_awareness() -> DpiAwareness:
    """Make the process Per-Monitor (V2) DPI aware, and report what it ended up as.

    Idempotent. The process default can be set once; a second call — ours or
    anyone's — is refused with `ERROR_ACCESS_DENIED`, which is not a failure if
    the awareness already in force is per-monitor. It never raises: a core that
    cannot be made aware still serves everything that is not perception, and
    `query_layout()` is what refuses to produce a coordinate under it.
    """
    ok, error = win32.set_process_dpi_awareness_context(
        win32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    )
    awareness = current_dpi_awareness()
    if not ok and error != win32.ERROR_ACCESS_DENIED:
        log.error("display.dpi_awareness_failed", extra={"win32_error": error})
    if awareness is DpiAwareness.PER_MONITOR:
        log.info("display.dpi_aware", extra={"awareness": awareness.name, "set_here": ok})
    else:
        log.error("display.dpi_unaware", extra={"awareness": awareness.name})
    return awareness


def _read_monitors() -> tuple[Monitor, ...]:
    """Every connected monitor, primary first, then top-to-bottom, left-to-right."""
    try:
        handles = win32.enum_display_monitors(MAX_MONITORS)
    except OSError as error:
        raise DisplayError(f"Windows would not list the monitors ({error.winerror}).") from error
    if len(handles) > MAX_MONITORS:
        raise DisplayError(f"More than {MAX_MONITORS} monitors are connected.")

    monitors: list[Monitor] = []
    for handle in handles:
        info = win32.monitor_info(handle)
        dpi = win32.monitor_dpi(handle)
        if info is None or dpi is None:
            # Enumerated a moment ago and gone now: the layout is mid-change.
            raise DisplayError("A monitor disappeared while the layout was being read.")
        monitors.append(
            Monitor(
                device=info.szDevice,
                bounds=_rect(info.rcMonitor),
                work_area=_rect(info.rcWork),
                dpi=dpi,
                primary=bool(info.dwFlags & win32.MONITORINFOF_PRIMARY),
            )
        )
    monitors.sort(key=lambda m: (not m.primary, m.bounds.top, m.bounds.left, m.device))
    return tuple(monitors)


def _read_virtual() -> Rect:
    left, top, width, height = win32.virtual_screen()
    return Rect(left, top, left + width, top + height)


def _rect(rect: wintypes.RECT) -> Rect:
    return Rect(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def query_layout() -> DisplayLayout:
    """The monitors as they are right now, in physical virtual-desktop pixels.

    Cheap enough to call before every action (a few Win32 calls, no I/O), which
    is the intended use: never cache a layout across a step.

    Raises `DpiAwarenessError` if the calling thread would be handed virtualised
    coordinates, and `DisplayError` if the layout will not hold still long enough
    to be read consistently.
    """
    awareness = current_dpi_awareness()
    if awareness is not DpiAwareness.PER_MONITOR:
        raise DpiAwarenessError(
            f"This thread is {awareness.name} DPI aware, so Windows would report scaled "
            "coordinates. Screen positions are only read per-monitor aware."
        )
    failure: DisplayError | None = None
    for _ in range(QUERY_ATTEMPTS):
        try:
            return DisplayLayout(monitors=_read_monitors(), virtual=_read_virtual())
        except DisplayError as error:
            failure = error
    raise DisplayError(
        f"The display layout could not be read consistently ({failure})."
    ) from failure


def verify_layout(expected: DisplayLayout) -> DisplayLayout:
    """Re-read the layout and refuse if it is not the one a position was measured under.

    Returns the fresh layout so the caller acts on current numbers. On a change it
    logs what changed in outline — counts and fingerprints, which carry no content
    — and raises, so the caller takes a new observation instead of reusing a box.
    """
    current = query_layout()
    if current != expected:
        log.warning(
            "display.layout_changed",
            extra={
                "was": expected.fingerprint,
                "now": current.fingerprint,
                "monitors_was": len(expected.monitors),
                "monitors_now": len(current.monitors),
            },
        )
        raise LayoutChangedError(
            "The display layout changed since this position was measured. "
            "A fresh observation is needed."
        )
    return current
