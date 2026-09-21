"""The virtual-desktop coordinate model (`P2-01`).

Three layers, from cheapest to most real:

* the pure model — `Rect`, `DisplayLayout` validation, `monitor_at` and the
  `SendInput` mapping — over hand-built mixed-DPI layouts with negative
  coordinates and dead zones, which no single developer machine has;
* the live Win32 reads, against whatever monitors this machine really has;
* a real cursor round-trip: every point is sent through `SendInput` as an
  absolute move and read back with `GetCursorPos`. That is the only test that can
  prove `to_absolute` agrees with Windows rather than with itself.
"""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Win32 display APIs")

from aegis_core.actuation import win32 as input_win32  # noqa: E402
from aegis_core.actuation.signature import AEGIS_SIGNATURE  # noqa: E402
from aegis_core.perception import display  # noqa: E402
from aegis_core.perception import win32 as display_win32  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    ABSOLUTE_SPAN,
    MAX_MONITORS,
    DisplayError,
    DisplayLayout,
    DpiAwareness,
    DpiAwarenessError,
    LayoutChangedError,
    Monitor,
    OffScreenError,
    Point,
    Rect,
    ensure_dpi_awareness,
    query_layout,
    verify_layout,
)


@pytest.fixture(autouse=True, scope="module")
def _per_monitor_aware() -> None:
    """What `__main__` does first. Process-wide and permanent, as it is in the core."""
    ensure_dpi_awareness()


def monitor(
    device: str,
    bounds: Rect,
    *,
    dpi: int = 96,
    primary: bool = False,
    work_area: Rect | None = None,
) -> Monitor:
    return Monitor(
        device=device,
        bounds=bounds,
        work_area=work_area if work_area is not None else bounds,
        dpi=dpi,
        primary=primary,
    )


#: A realistic awkward desk: a 150 % laptop panel as primary, a 100 % monitor to
#: its left and 200 px lower, and a 200 % 4K monitor above. The bounding box has
#: dead zones at its top-left and bottom-right that no monitor shows.
PRIMARY = monitor("\\\\.\\DISPLAY1", Rect(0, 0, 2560, 1440), dpi=144, primary=True)
LEFT = monitor("\\\\.\\DISPLAY2", Rect(-1920, 200, 0, 1280), dpi=96)
ABOVE = monitor("\\\\.\\DISPLAY3", Rect(0, -2160, 3840, 0), dpi=192)
MIXED = DisplayLayout(monitors=(PRIMARY, LEFT, ABOVE), virtual=Rect(-1920, -2160, 3840, 1440))


#: `GetSystemMetrics` index for the primary monitor's width.
SM_CXSCREEN = 0


def windows_pixel(absolute: int, origin: int, span: int) -> int:
    """How Windows maps an absolute `SendInput` coordinate back to a pixel."""
    return origin + (absolute * span) // ABSOLUTE_SPAN


# --------------------------------------------------------------------------- #
# Rect
# --------------------------------------------------------------------------- #


def test_a_rect_is_exclusive_on_its_right_and_bottom_edges() -> None:
    rect = Rect(0, 0, 1920, 1080)
    assert (rect.width, rect.height) == (1920, 1080)
    assert rect.contains(Point(0, 0))
    assert rect.contains(Point(1919, 1079))
    assert not rect.contains(Point(1920, 0))
    assert not rect.contains(Point(0, 1080))
    assert not rect.contains(Point(-1, 0))


def test_an_inverted_or_zero_sized_rect_is_empty() -> None:
    assert Rect(10, 10, 10, 20).is_empty
    assert Rect(10, 10, 20, 10).is_empty
    assert Rect(20, 20, 10, 10).is_empty
    assert not Rect(0, 0, 1, 1).is_empty


def test_contains_rect_allows_touching_edges_and_refuses_overhang() -> None:
    outer = Rect(0, 0, 100, 100)
    assert outer.contains_rect(Rect(0, 0, 100, 100))
    assert outer.contains_rect(Rect(10, 10, 90, 90))
    assert not outer.contains_rect(Rect(-1, 0, 100, 100))
    assert not outer.contains_rect(Rect(0, 0, 101, 100))


# --------------------------------------------------------------------------- #
# Layout validation — a DisplayLayout that exists is one that agrees with itself
# --------------------------------------------------------------------------- #


def test_a_mixed_dpi_layout_with_negative_coordinates_is_valid() -> None:
    assert MIXED.primary is PRIMARY
    assert [m.scale for m in MIXED.monitors] == [1.5, 1.0, 2.0]


def test_no_monitors_is_refused() -> None:
    with pytest.raises(DisplayError, match="no monitors"):
        DisplayLayout(monitors=(), virtual=Rect(0, 0, 1, 1))


def test_more_than_the_cap_is_refused() -> None:
    monitors = tuple(
        monitor(f"D{i}", Rect(i * 100, 0, i * 100 + 100, 100), primary=i == 0)
        for i in range(MAX_MONITORS + 1)
    )
    with pytest.raises(DisplayError, match="More than"):
        DisplayLayout(monitors=monitors, virtual=Rect(0, 0, (MAX_MONITORS + 1) * 100, 100))


@pytest.mark.parametrize("primaries", [0, 2])
def test_exactly_one_primary_is_required(primaries: int) -> None:
    first = monitor("A", Rect(0, 0, 100, 100), primary=primaries >= 1)
    second = monitor("B", Rect(100, 0, 200, 100), primary=primaries >= 2)
    with pytest.raises(DisplayError, match="one primary"):
        DisplayLayout(monitors=(first, second), virtual=Rect(0, 0, 200, 100))


def test_a_primary_away_from_the_origin_is_refused() -> None:
    shifted = monitor("A", Rect(10, 0, 110, 100), primary=True)
    with pytest.raises(DisplayError, match="origin"):
        DisplayLayout(monitors=(shifted,), virtual=Rect(10, 0, 110, 100))


def test_an_empty_monitor_is_refused() -> None:
    empty = monitor("B", Rect(100, 0, 100, 100))
    with pytest.raises(DisplayError, match="empty area"):
        DisplayLayout(monitors=(PRIMARY, empty), virtual=Rect(0, 0, 2560, 1440))


def test_a_work_area_outside_its_monitor_is_refused() -> None:
    bad = monitor("A", Rect(0, 0, 100, 100), primary=True, work_area=Rect(0, 0, 100, 101))
    with pytest.raises(DisplayError, match="work area"):
        DisplayLayout(monitors=(bad,), virtual=Rect(0, 0, 100, 100))


@pytest.mark.parametrize("dpi", [0, 72, 95, 481, 10_000])
def test_a_dpi_windows_cannot_produce_is_refused(dpi: int) -> None:
    bad = monitor("A", Rect(0, 0, 100, 100), primary=True, dpi=dpi)
    with pytest.raises(DisplayError, match="DPI"):
        DisplayLayout(monitors=(bad,), virtual=Rect(0, 0, 100, 100))


@pytest.mark.parametrize("dpi", [96, 120, 144, 168, 192, 240, 288, 480])
def test_every_scaling_windows_offers_is_accepted(dpi: int) -> None:
    layout = DisplayLayout(
        monitors=(monitor("A", Rect(0, 0, 100, 100), primary=True, dpi=dpi),),
        virtual=Rect(0, 0, 100, 100),
    )
    assert layout.primary.dpi == dpi


@pytest.mark.parametrize(
    "virtual",
    [
        Rect(-1920, -2160, 3840, 1441),  # one row too tall
        Rect(-1920, 0, 3840, 1440),  # missing the monitor above
        Rect(0, 0, 2560, 1440),  # the primary only: a monitor was added mid-read
    ],
)
def test_a_virtual_desktop_that_is_not_the_bounding_box_is_refused(virtual: Rect) -> None:
    with pytest.raises(DisplayError, match="does not match"):
        DisplayLayout(monitors=MIXED.monitors, virtual=virtual)


# --------------------------------------------------------------------------- #
# monitor_at — negative coordinates are real, dead zones are not
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (Point(0, 0), PRIMARY),
        (Point(2559, 1439), PRIMARY),
        (Point(-1, 200), LEFT),
        (Point(-1920, 1279), LEFT),
        (Point(0, -1), ABOVE),
        (Point(3839, -2160), ABOVE),
    ],
)
def test_monitor_at_finds_the_monitor_including_negative_coordinates(
    point: Point, expected: Monitor
) -> None:
    assert MIXED.monitor_at(point) is expected


@pytest.mark.parametrize(
    "point",
    [
        Point(-1, 0),  # left of the primary, above the left monitor's top
        Point(-1920, -1),  # top-left dead zone of the bounding box
        Point(3000, 100),  # right of the primary, below the monitor above
        Point(-1, 1280),  # just below the left monitor
    ],
)
def test_a_dead_zone_inside_the_bounding_box_is_on_no_monitor(point: Point) -> None:
    assert MIXED.virtual.contains(point)
    assert MIXED.monitor_at(point) is None


@pytest.mark.parametrize("point", [Point(3840, -1), Point(0, 1440), Point(-1921, 300)])
def test_a_point_outside_the_virtual_desktop_is_on_no_monitor(point: Point) -> None:
    assert not MIXED.virtual.contains(point)
    assert MIXED.monitor_at(point) is None


# --------------------------------------------------------------------------- #
# to_absolute — the SendInput mapping, exact for every pixel
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("span", [1, 2, 3, 7, 1366, 1920, 2560, 3200, 5760, 7680, 11_520, 32_768])
def test_every_pixel_on_an_axis_survives_windows_mapping(span: int) -> None:
    """Exhaustive over the axis: the midpoint rule hits every pixel exactly."""
    for origin in (0, -span // 2):
        values = [display._absolute(offset, span) for offset in range(span)]
        assert values == sorted(values)
        assert values[0] >= 0
        assert values[-1] < ABSOLUTE_SPAN
        for offset, absolute in enumerate(values):
            assert windows_pixel(absolute, origin, span) == origin + offset


def test_to_absolute_maps_both_axes_against_the_virtual_origin() -> None:
    for point in (Point(0, 0), Point(-1920, 200), Point(3839, -2160), Point(2559, 1439)):
        ax, ay = MIXED.to_absolute(point)
        assert windows_pixel(ax, MIXED.virtual.left, MIXED.virtual.width) == point.x
        assert windows_pixel(ay, MIXED.virtual.top, MIXED.virtual.height) == point.y


@pytest.mark.parametrize("point", [Point(-1, 0), Point(3000, 100), Point(99_999, 0)])
def test_to_absolute_refuses_a_point_on_no_monitor(point: Point) -> None:
    """Windows would clamp it onto some edge; the model refuses instead."""
    with pytest.raises(OffScreenError, match="not on any monitor"):
        MIXED.to_absolute(point)


# --------------------------------------------------------------------------- #
# Fingerprint and change detection
# --------------------------------------------------------------------------- #


def test_equal_layouts_share_a_fingerprint() -> None:
    again = DisplayLayout(monitors=MIXED.monitors, virtual=MIXED.virtual)
    assert again == MIXED
    assert again.fingerprint == MIXED.fingerprint
    assert len(MIXED.fingerprint) == 16


def test_a_scaling_change_alone_changes_the_fingerprint() -> None:
    """Physical bounds stay put when scaling changes, but every window moves."""
    rescaled = DisplayLayout(
        monitors=(dataclasses.replace(PRIMARY, dpi=192), LEFT, ABOVE),
        virtual=MIXED.virtual,
    )
    assert rescaled != MIXED
    assert rescaled.fingerprint != MIXED.fingerprint


def test_swapping_the_primary_changes_the_fingerprint() -> None:
    first = DisplayLayout(
        monitors=(monitor("A", Rect(0, 0, 100, 100), primary=True),),
        virtual=Rect(0, 0, 100, 100),
    )
    second = DisplayLayout(
        monitors=(monitor("B", Rect(0, 0, 100, 100), primary=True),),
        virtual=Rect(0, 0, 100, 100),
    )
    assert first.fingerprint != second.fingerprint


def test_verify_layout_returns_the_fresh_layout_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = DisplayLayout(monitors=MIXED.monitors, virtual=MIXED.virtual)
    monkeypatch.setattr(display, "query_layout", lambda: fresh)
    assert verify_layout(MIXED) is fresh


def test_verify_layout_refuses_and_logs_when_a_monitor_was_unplugged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    unplugged = DisplayLayout(monitors=(PRIMARY,), virtual=PRIMARY.bounds)
    monkeypatch.setattr(display, "query_layout", lambda: unplugged)
    with (
        caplog.at_level(logging.WARNING, logger=display.__name__),
        pytest.raises(LayoutChangedError, match="fresh observation"),
    ):
        verify_layout(MIXED)
    record = next(r for r in caplog.records if r.message == "display.layout_changed")
    assert record.__dict__["monitors_was"] == 3
    assert record.__dict__["monitors_now"] == 1
    assert record.__dict__["was"] == MIXED.fingerprint


# --------------------------------------------------------------------------- #
# query_layout — retries a torn read, refuses an unaware thread
# --------------------------------------------------------------------------- #


def test_a_read_torn_by_a_display_change_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    virtuals = iter([Rect(0, 0, 2560, 1440), MIXED.virtual])
    monkeypatch.setattr(display, "_read_monitors", lambda: MIXED.monitors)
    monkeypatch.setattr(display, "_read_virtual", lambda: next(virtuals))
    assert query_layout() == MIXED


def test_a_layout_that_never_holds_still_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def torn() -> Rect:
        calls.append(1)
        return Rect(0, 0, 1, 1)

    monkeypatch.setattr(display, "_read_monitors", lambda: MIXED.monitors)
    monkeypatch.setattr(display, "_read_virtual", torn)
    with pytest.raises(DisplayError, match="could not be read consistently"):
        query_layout()
    assert len(calls) == display.QUERY_ATTEMPTS


def test_a_monitor_that_vanishes_mid_read_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_win32, "monitor_info", lambda _handle: None)
    with pytest.raises(DisplayError, match="disappeared"):
        display._read_monitors()


def test_too_many_monitors_from_windows_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        display_win32, "enum_display_monitors", lambda limit: list(range(limit + 1))
    )
    with pytest.raises(DisplayError, match="More than"):
        display._read_monitors()


def _in_thread_with_awareness(context: int) -> BaseException | DisplayLayout:
    """Run `query_layout()` on a fresh thread whose awareness is overridden."""
    outcome: list[BaseException | DisplayLayout] = []

    def run() -> None:
        assert display_win32.set_thread_dpi_awareness_context(context) is not None
        try:
            outcome.append(query_layout())
        except BaseException as error:  # handed back to the test thread
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return outcome[0]


def test_an_unaware_thread_is_refused_a_layout() -> None:
    """Handing it coordinates would hand it the virtualised (scaled) desktop."""
    ensure_dpi_awareness()
    outcome = _in_thread_with_awareness(display_win32.DPI_AWARENESS_CONTEXT_UNAWARE)
    assert isinstance(outcome, DpiAwarenessError)
    assert "UNAWARE" in str(outcome)


# --------------------------------------------------------------------------- #
# Live: this machine's real monitors
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_layout() -> DisplayLayout:
    assert ensure_dpi_awareness() is DpiAwareness.PER_MONITOR
    return query_layout()


def test_ensure_dpi_awareness_is_idempotent_and_ends_per_monitor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=display.__name__):
        assert ensure_dpi_awareness() is DpiAwareness.PER_MONITOR
        assert ensure_dpi_awareness() is DpiAwareness.PER_MONITOR
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert display.current_dpi_awareness() is DpiAwareness.PER_MONITOR


def test_the_live_layout_is_valid_and_stable(live_layout: DisplayLayout) -> None:
    assert live_layout.primary.bounds.left == 0
    assert live_layout.primary.bounds.top == 0
    assert live_layout.monitors[0].primary
    assert query_layout() == live_layout
    assert verify_layout(live_layout) == live_layout
    for m in live_layout.monitors:
        assert m.device.startswith("\\\\.\\")
        assert live_layout.monitor_at(Point(m.bounds.left, m.bounds.top)) == m


def test_the_live_layout_is_in_physical_pixels(live_layout: DisplayLayout) -> None:
    """An unaware thread sees the primary shrunk by its scale; the layout must not.

    This is the bug the whole task exists to prevent, observed directly. Only
    meaningful on a scaled primary: at 100 % both views agree.
    """
    primary = live_layout.primary
    if primary.dpi == display.BASE_DPI:
        pytest.skip("the primary monitor is at 100 %, so logical and physical agree")
    seen: list[int] = []

    def unaware() -> None:
        display_win32.set_thread_dpi_awareness_context(display_win32.DPI_AWARENESS_CONTEXT_UNAWARE)
        seen.append(int(display_win32.user32.GetSystemMetrics(SM_CXSCREEN)))

    thread = threading.Thread(target=unaware)
    thread.start()
    thread.join(timeout=10)
    assert seen == [round(primary.bounds.width / primary.scale)]
    assert seen[0] < primary.bounds.width


def test_querying_the_layout_is_cheap_enough_to_do_before_every_action(
    live_layout: DisplayLayout,
) -> None:
    runs = 50
    started = time.perf_counter()
    for _ in range(runs):
        query_layout()
    per_call_ms = (time.perf_counter() - started) * 1000 / runs
    assert per_call_ms < 5.0, f"{per_call_ms:.2f} ms per query"


# --------------------------------------------------------------------------- #
# Live: the cursor lands exactly where to_absolute says
# --------------------------------------------------------------------------- #


class _POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetCursorPos.argtypes = (ctypes.POINTER(_POINT),)
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
_user32.SetCursorPos.restype = wintypes.BOOL


def _cursor() -> Point | None:
    where = _POINT()
    if not _user32.GetCursorPos(ctypes.byref(where)):
        return None
    return Point(int(where.x), int(where.y))


def _move_absolute(layout: DisplayLayout, point: Point) -> None:
    ax, ay = layout.to_absolute(point)
    event = input_win32.INPUT(type=input_win32.INPUT_MOUSE)
    event.u.mi = input_win32.MOUSEINPUT(
        dx=ax,
        dy=ay,
        mouseData=0,
        dwFlags=(
            input_win32.MOUSEEVENTF_MOVE
            | input_win32.MOUSEEVENTF_ABSOLUTE
            | input_win32.MOUSEEVENTF_VIRTUALDESK
        ),
        time=0,
        dwExtraInfo=AEGIS_SIGNATURE,
    )
    input_win32.send_input([event])


@pytest.fixture
def restored_cursor() -> Iterator[Point]:
    start = _cursor()
    if start is None:
        pytest.skip("no interactive desktop: GetCursorPos is refused")
    try:
        yield start
    finally:
        _user32.SetCursorPos(start.x, start.y)


def _probe_points(layout: DisplayLayout) -> list[Point]:
    """Every corner and the centre of every monitor, plus a sweep along each axis."""
    points: list[Point] = []
    for m in layout.monitors:
        b = m.bounds
        points += [
            Point(b.left, b.top),
            Point(b.right - 1, b.top),
            Point(b.left, b.bottom - 1),
            Point(b.right - 1, b.bottom - 1),
            Point((b.left + b.right) // 2, (b.top + b.bottom) // 2),
        ]
        points += [Point(x, b.top + b.height // 3) for x in range(b.left, b.right, 37)]
        points += [Point(b.left + b.width // 3, y) for y in range(b.top, b.bottom, 23)]
    return points


def test_an_absolute_move_lands_on_exactly_the_pixel_asked_for(
    live_layout: DisplayLayout, restored_cursor: Point
) -> None:
    """Round-trip through Windows itself: SendInput out, GetCursorPos back.

    A point is retried once on a miss, because a human nudging the mouse during
    the run is indistinguishable from a wrong mapping on a single sample; a
    wrong mapping misses every time.
    """
    misses: list[tuple[Point, Point | None]] = []
    for point in _probe_points(live_layout):
        for _attempt in range(2):
            _move_absolute(live_layout, point)
            landed = _cursor()
            if landed == point:
                break
        else:
            misses.append((point, landed))
    assert not misses, f"{len(misses)} points missed, e.g. {misses[:5]}"
