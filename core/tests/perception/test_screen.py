"""Tests for `perception/screen.py` — capture, downscale, WebP.

Three layers, like `test_display.py`:

* pure — sizing, channel order and encoding on hand-built frames;
* resolution — which rectangle a target means, and every refusal, against
  synthetic layouts with the Win32 and `mss` calls replaced;
* live — this machine's real screen, including a red window the test opens itself.

The < 150 ms budget from `PROGRESS.md` P2-02 is held two ways (P2-10). The
default suite times the downscale and encode — this module's own work — on a
fixed, deliberately busy frame, so the number cannot depend on what happens to
be on screen. The whole capture on the live screen is a benchmark, run with
`AEGIS_PERF=1` on a quiet machine: it measures Windows and the machine's load as
much as this code, so it cannot be a gate on a developer's busy desktop.
"""

from __future__ import annotations

import io
import os
import random
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes
from typing import Any

import mss
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Win32 capture APIs")

from aegis_core.perception import display, screen  # noqa: E402
from aegis_core.perception import win32 as display_win32  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    DpiAwareness,
    DpiAwarenessError,
    LayoutChangedError,
    Monitor,
    OffScreenError,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.screen import (  # noqa: E402
    MAX_CAPTURE_PIXELS,
    MAX_EDGE,
    CaptureError,
    Frame,
    MonitorTarget,
    RegionTarget,
    Screenshot,
    WindowTarget,
    capture,
    fit_within,
)
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from tests.perception.helpers import SolidWindow  # noqa: E402

RED = (255, 0, 0)
BLUE = (0, 0, 255)

#: `PROGRESS.md` P2-02.
BUDGET_MS = 150.0

#: Opts into the live capture benchmark.
PERF_ENV = "AEGIS_PERF"


@pytest.fixture(autouse=True, scope="module")
def _per_monitor_aware() -> None:
    ensure_dpi_awareness()


def monitor(
    device: str, left: int, top: int, width: int, height: int, *, primary: bool = False
) -> Monitor:
    bounds = Rect(left, top, left + width, top + height)
    return Monitor(device=device, bounds=bounds, work_area=bounds, dpi=96, primary=primary)


def layout(*monitors: Monitor) -> DisplayLayout:
    return DisplayLayout(
        monitors=monitors,
        virtual=Rect(
            min(m.bounds.left for m in monitors),
            min(m.bounds.top for m in monitors),
            max(m.bounds.right for m in monitors),
            max(m.bounds.bottom for m in monitors),
        ),
    )


#: A 1920x1080 primary, a 1280x1024 monitor to its left sitting lower, so the
#: bounding box has a dead zone at its top-left: x in [-1280, 0), y in [0, 56).
DESK = layout(
    monitor("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, primary=True),
    monitor("\\\\.\\DISPLAY2", -1280, 56, 1280, 1024),
)


def bgrx(width: int, height: int, rgb: tuple[int, int, int]) -> bytearray:
    red, green, blue = rgb
    return bytearray(bytes((blue, green, red, 0)) * (width * height))


def frame_of(width: int, height: int, rgb: tuple[int, int, int] = RED) -> Frame:
    return Frame(
        region=Rect(0, 0, width, height),
        layout=DESK,
        captured_at=0.0,
        pixels=bgrx(width, height, rgb),
    )


def decode(shot: Screenshot) -> Image.Image:
    image = Image.open(io.BytesIO(shot.data))
    assert image.format == "WEBP"
    return image.convert("RGB")


def close_to(actual: Any, expected: tuple[int, int, int], tolerance: int = 24) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


# --------------------------------------------------------------------------- #
# Pure: sizing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((3200, 2000), (1280, 800)),
        ((2000, 3200), (800, 1280)),
        ((3840, 2160), (1280, 720)),
        ((1280, 1024), (1280, 1024)),
        ((640, 480), (640, 480)),
        ((1, 1), (1, 1)),
        ((20_000, 1), (1280, 1)),
        ((1, 20_000), (1, 1280)),
    ],
)
def test_fit_within_scales_the_long_side_to_the_limit(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert fit_within(*size, MAX_EDGE) == expected


def test_fit_within_never_exceeds_the_limit_and_keeps_the_aspect_ratio() -> None:
    for width in range(1, 8000, 37):
        for height in (1, 7, 600, 1081, 2000, 4321):
            fitted = fit_within(width, height, MAX_EDGE)
            assert max(fitted) <= MAX_EDGE
            assert min(fitted) >= 1
            assert fitted[0] <= width and fitted[1] <= height
            if max(width, height) > MAX_EDGE:
                assert max(fitted) == MAX_EDGE
                # Within one pixel of the exact ratio on the short side.
                exact = min(width, height) * MAX_EDGE / max(width, height)
                assert abs(min(fitted) - max(1.0, exact)) <= 1


@pytest.mark.parametrize("max_edge", [0, -1, MAX_EDGE + 1, 4096])
def test_a_max_edge_beyond_the_architecture_limit_is_refused(max_edge: int) -> None:
    """`ARCHITECTURE.md § 6.2`: never more than 1280 px on the long edge."""
    with pytest.raises(ValueError, match="max_edge"):
        fit_within(100, 100, max_edge)
    with pytest.raises(ValueError, match="max_edge"):
        frame_of(4, 4)._encode(max_edge=max_edge)


def test_fit_within_refuses_an_empty_size() -> None:
    with pytest.raises(ValueError, match="Cannot fit"):
        fit_within(0, 10, MAX_EDGE)


# --------------------------------------------------------------------------- #
# Pure: frames
# --------------------------------------------------------------------------- #


def test_a_frame_whose_buffer_does_not_match_its_region_is_refused() -> None:
    with pytest.raises(CaptureError, match="needs 64 bytes"):
        Frame(region=Rect(0, 0, 4, 4), layout=DESK, captured_at=0.0, pixels=bytearray(63))


def test_an_empty_frame_is_refused() -> None:
    with pytest.raises(CaptureError, match="empty"):
        Frame(region=Rect(0, 0, 0, 4), layout=DESK, captured_at=0.0, pixels=bytearray())


def test_neither_repr_contains_the_pixels() -> None:
    """The pixels are the user's screen; a logged repr must not carry them."""
    frame = frame_of(8, 8)
    shot = frame._encode()
    assert "pixels" not in repr(frame)
    assert "bytearray" not in repr(frame)
    assert "data" not in repr(shot)
    assert repr(shot.data[:8]) not in repr(shot)


@pytest.mark.parametrize("rgb", [RED, BLUE, (0, 255, 0), (12, 34, 56)])
def test_gdi_bgrx_comes_out_as_rgb(rgb: tuple[int, int, int]) -> None:
    """The one place a swapped channel would turn every red button blue."""
    assert frame_of(3, 2, rgb)._to_image().getpixel((1, 1)) == rgb


@pytest.mark.parametrize("rgb", [RED, BLUE])
def test_channel_order_survives_the_downscale(rgb: tuple[int, int, int]) -> None:
    image = frame_of(2560, 1600, rgb)._to_image()
    assert image.size == (1280, 800)
    assert image.mode == "RGB"
    assert image.getpixel((640, 400)) == rgb


def test_the_downscale_keeps_left_left_and_top_top() -> None:
    """A red left half and a blue bottom-right quarter land where they were."""
    width, height = 2560, 1600
    pixels = bytearray()
    for y in range(height):
        left = bytes((0, 0, 255, 0)) * (width // 2)
        right = (bytes((255, 0, 0, 0)) if y >= height // 2 else bytes((0, 255, 0, 0))) * (
            width // 2
        )
        pixels += left + right
    frame = Frame(region=Rect(0, 0, width, height), layout=DESK, captured_at=0.0, pixels=pixels)
    image = frame._to_image()
    assert image.getpixel((100, 100)) == RED
    assert image.getpixel((100, 700)) == RED
    assert image.getpixel((1200, 100)) == (0, 255, 0)
    assert image.getpixel((1200, 700)) == BLUE


def test_a_small_frame_is_never_upscaled() -> None:
    assert frame_of(300, 200)._to_image().size == (300, 200)


def test_encode_produces_a_decodable_webp_that_says_where_it_came_from() -> None:
    frame = Frame(
        region=Rect(-1280, 56, 1280, 1080),
        layout=DESK,
        captured_at=12.5,
        pixels=bgrx(2560, 1024, RED),
    )
    shot = frame._encode()
    assert shot.media_type == "image/webp"
    assert (shot.width, shot.height) == (1280, 512)
    assert shot.region == frame.region
    assert shot.layout_fingerprint == DESK.fingerprint
    assert shot.captured_at == 12.5
    image = decode(shot)
    assert image.size == (1280, 512)
    assert close_to(image.getpixel((640, 256)), RED)


def test_encode_honours_a_smaller_max_edge() -> None:
    shot = frame_of(1000, 500)._encode(max_edge=200)
    assert (shot.width, shot.height) == (200, 100)
    assert decode(shot).size == (200, 100)


@pytest.mark.parametrize("quality", [0, 101, -5])
def test_an_impossible_quality_is_refused(quality: int) -> None:
    with pytest.raises(ValueError, match="quality"):
        frame_of(4, 4)._encode(quality=quality)


def test_pillow_was_built_with_webp() -> None:
    """A Pillow without libwebp would pass every import and fail the first real step."""
    from PIL import features

    assert features.check("webp")


# --------------------------------------------------------------------------- #
# Resolution: which rectangle a target means, and every refusal
# --------------------------------------------------------------------------- #


class _Grabs:
    """Stands in for `mss`: records each region and returns blank BGRX pixels."""

    def __init__(self) -> None:
        self.regions: list[Rect] = []

    def __call__(self, region: Rect) -> bytearray:
        self.regions.append(region)
        return bytearray(region.width * region.height * 4)


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> _Grabs:
    """`DESK` as the live layout before and after the grab, and a fake grab."""
    grabs = _Grabs()
    monkeypatch.setattr(screen, "query_layout", lambda: DESK)
    monkeypatch.setattr(display, "query_layout", lambda: DESK)
    monkeypatch.setattr(screen, "_grab", grabs)
    return grabs


def test_the_default_target_is_the_primary_monitor(desk: _Grabs) -> None:
    frame = capture()
    assert frame.region == Rect(0, 0, 1920, 1080)
    assert frame.layout is DESK
    assert desk.regions == [Rect(0, 0, 1920, 1080)]


def test_a_monitor_is_found_by_device_name_including_negative_coordinates(desk: _Grabs) -> None:
    assert capture(MonitorTarget("\\\\.\\DISPLAY2")).region == Rect(-1280, 56, 0, 1080)


def test_an_unknown_monitor_is_refused_before_anything_is_grabbed(desk: _Grabs) -> None:
    with pytest.raises(CaptureError, match="DISPLAY9"):
        capture(MonitorTarget("\\\\.\\DISPLAY9"))
    assert desk.regions == []


def test_a_region_across_the_dead_zone_is_allowed(desk: _Grabs) -> None:
    region = Rect(-100, 0, 100, 100)
    assert capture(RegionTarget(region)).region == region


@pytest.mark.parametrize(
    ("region", "error", "message"),
    [
        (Rect(10, 10, 10, 50), CaptureError, "empty"),
        (Rect(50, 50, 10, 60), CaptureError, "empty"),
        (Rect(1900, 0, 1930, 10), OffScreenError, "edge of the desktop"),
        (Rect(-1300, 100, -1200, 200), OffScreenError, "edge of the desktop"),
        (Rect(0, -1, 10, 10), OffScreenError, "edge of the desktop"),
        (Rect(-1000, 0, -900, 50), OffScreenError, "not on any monitor"),
    ],
)
def test_a_region_that_shows_nothing_real_is_refused(
    desk: _Grabs, region: Rect, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        capture(RegionTarget(region))
    assert desk.regions == []


def test_a_region_over_the_pixel_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`REVIEW.md § 2`: no unbounded buffer, even on a desk that could supply one."""
    wide = layout(
        monitor("\\\\.\\DISPLAY1", 0, 0, 7680, 4320, primary=True),
        monitor("\\\\.\\DISPLAY2", 7680, 0, 7680, 4320),
    )
    grabs = _Grabs()
    monkeypatch.setattr(screen, "query_layout", lambda: wide)
    monkeypatch.setattr(screen, "_grab", grabs)
    assert MAX_CAPTURE_PIXELS < 7681 * 4320
    with pytest.raises(CaptureError, match="capture limit"):
        capture(RegionTarget(Rect(0, 0, 7681, 4320)))
    assert grabs.regions == []


def test_a_monitor_change_during_the_grab_voids_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RECOVERY.md § 3.3`: never hand back a region measured on a screen that is gone."""
    unplugged = layout(monitor("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, primary=True))
    grabs = _Grabs()
    monkeypatch.setattr(screen, "query_layout", lambda: DESK)
    monkeypatch.setattr(display, "query_layout", lambda: unplugged)
    monkeypatch.setattr(screen, "_grab", grabs)
    with pytest.raises(LayoutChangedError):
        capture()
    assert len(grabs.regions) == 1


class _FakeWindow:
    """What the Win32 window queries answer for one pretend `HWND`."""

    def __init__(
        self,
        *,
        exists: bool = True,
        minimised: bool = False,
        visible: bool = True,
        cloaked: bool = False,
        bounds: tuple[int, int, int, int] | None = (100, 100, 900, 700),
    ) -> None:
        self.exists = exists
        self.minimised = minimised
        self.visible = visible
        self.cloaked = cloaked
        self.bounds = bounds

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(display_win32, "is_window", lambda _h: self.exists)
        monkeypatch.setattr(display_win32, "is_minimised", lambda _h: self.minimised)
        monkeypatch.setattr(display_win32, "is_window_visible", lambda _h: self.visible)
        monkeypatch.setattr(display_win32, "is_cloaked", lambda _h: self.cloaked)
        monkeypatch.setattr(display_win32, "window_bounds", self._bounds)

    def _bounds(self, _hwnd: int) -> wintypes.RECT | None:
        return None if self.bounds is None else wintypes.RECT(*self.bounds)


def test_a_window_is_captured_as_its_drawn_bounds(
    desk: _Grabs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeWindow().install(monkeypatch)
    assert capture(WindowTarget(0x1234)).region == Rect(100, 100, 900, 700)


def test_a_window_hanging_off_the_desktop_is_clipped_to_it(
    desk: _Grabs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeWindow(bounds=(1500, -40, 2400, 500)).install(monkeypatch)
    assert capture(WindowTarget(0x1234)).region == Rect(1500, 0, 1920, 500)


@pytest.mark.parametrize(
    ("window", "error", "message"),
    [
        (_FakeWindow(exists=False), CaptureError, "no longer exists"),
        (_FakeWindow(minimised=True), CaptureError, "minimised"),
        (_FakeWindow(visible=False), CaptureError, "hidden or on another virtual desktop"),
        (_FakeWindow(cloaked=True), CaptureError, "hidden or on another virtual desktop"),
        (_FakeWindow(bounds=None), CaptureError, "would not say where"),
        (_FakeWindow(bounds=(3000, 0, 3500, 400)), OffScreenError, "entirely off the screen"),
        (_FakeWindow(bounds=(-1100, 0, -1000, 40)), OffScreenError, "not on any monitor"),
    ],
)
def test_a_window_with_nothing_on_screen_is_refused_with_the_reason(
    desk: _Grabs,
    monkeypatch: pytest.MonkeyPatch,
    window: _FakeWindow,
    error: type[Exception],
    message: str,
) -> None:
    """`REVIEW.md § 2`: minimised or on another virtual desktop fails with a clear message."""
    window.install(monkeypatch)
    with pytest.raises(error, match=message):
        capture(WindowTarget(0x1234))
    assert desk.regions == []


class _Refusing:
    """An `mss` object whose every grab fails, as GDI's does on a locked machine."""

    closed = 0

    def grab(self, _box: object) -> None:
        raise mss.ScreenShotError("BitBlt failed")

    def close(self) -> None:
        _Refusing.closed += 1


@pytest.fixture
def no_shared_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start with no cached `mss` object, and put the real one back afterwards."""
    monkeypatch.setattr(screen, "_screen", None)


@pytest.mark.usefixtures("no_shared_screen")
def test_a_refused_screen_copy_says_why_and_drops_the_broken_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GDI refuses a copy while the machine is locked or UAC's secure desktop is up.

    The object is dropped so that the first capture after unlocking builds fresh
    DCs, instead of failing forever on ones taken before the lock.
    """
    _Refusing.closed = 0
    monkeypatch.setattr(screen, "query_layout", lambda: DESK)
    monkeypatch.setattr(mss, "MSS", _Refusing)
    with pytest.raises(CaptureError, match="locked") as caught:
        capture()
    assert isinstance(caught.value.__cause__, mss.ScreenShotError)
    assert screen._screen is None
    assert _Refusing.closed == 1


@pytest.mark.usefixtures("no_shared_screen")
def test_one_mss_object_serves_every_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built once, lazily: a fresh one per call re-faults a 25 MiB DIB (~20 ms)."""
    built: list[object] = []
    real = mss.MSS

    def counting() -> mss.MSS:
        instance = real()
        built.append(instance)
        return instance

    monkeypatch.setattr(mss, "MSS", counting)
    capture(RegionTarget(Rect(0, 0, 8, 8)))
    capture(RegionTarget(Rect(8, 8, 40, 20)))
    capture()
    assert len(built) == 1
    assert screen._screen is built[0]


@pytest.mark.usefixtures("no_shared_screen")
def test_an_unaware_thread_is_refused_before_mss_can_touch_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mss` sets process DPI awareness when constructed; it must never get the chance."""
    constructed: list[object] = []

    def spy(*args: object, **kwargs: object) -> None:
        constructed.append(args)

    monkeypatch.setattr(mss, "MSS", spy)
    outcome: list[BaseException | Frame] = []

    def run() -> None:
        assert (
            display_win32.set_thread_dpi_awareness_context(
                display_win32.DPI_AWARENESS_CONTEXT_UNAWARE
            )
            is not None
        )
        try:
            outcome.append(capture())
        except BaseException as error:  # handed back to the test thread
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(outcome[0], DpiAwarenessError)
    assert constructed == []


# --------------------------------------------------------------------------- #
# Budget: the downscale and encode, on fixed content
# --------------------------------------------------------------------------- #


def busy_desktop(width: int = 3200, height: int = 2000) -> Frame:
    """A frame that is harder to encode than a real desktop: a title bar, a sidebar,
    dense lines of text and a photo-like noise panel.

    Measured on the dev machine: 236 KiB and ~68 ms to downscale and encode, against
    ~97 KiB and ~55 ms for the real 3200x2000 screen. Seeded, and drawn with Pillow's
    bundled font, so it is the same frame on every machine.
    """
    image = Image.new("RGB", (width, height), (243, 243, 243))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=26)
    draw.rectangle((0, 0, width, 90), fill=(32, 32, 32))
    draw.rectangle((0, 90, 560, height), fill=(225, 228, 232))
    rnd = random.Random(3)  # noqa: S311 - a fixed picture for a timing test, not secrets
    words = ("open", "file", "settings", "report", "invoice", "2026", "total", "Save", "view")
    for y in range(120, height - 40, 40):
        x = 600
        while x < width - 400:
            line = " ".join(rnd.choice(words) for _ in range(rnd.randint(2, 6)))
            draw.text((x, y), line, fill=(30, 30, 30), font=font)
            x += int(draw.textlength(line, font=font)) + 40
    for y in range(120, height - 40, 48):
        draw.text((30, y), rnd.choice(words).title(), fill=(60, 60, 60), font=font)
    image.paste(Image.effect_noise((900, 700), 60).convert("RGB"), (width - 1000, 200))
    region = Rect(0, 0, width, height)
    return Frame(
        region=region,
        layout=layout(Monitor("D0", region, region, 192, True)),
        captured_at=0.0,
        pixels=bytearray(image.tobytes("raw", "BGRX")),
    )


def test_downscaling_and_encoding_a_busy_desktop_fit_the_budget() -> None:
    """The half of P2-02's budget this module controls, on content that cannot vary.

    The ceiling is the **whole** 150 ms, not a share of it, because timing on a
    shared machine is never load-proof: with every logical CPU busy this took
    ~130 ms here, and even the thread's own CPU time doubled (hyperthreads share
    cores, turbo clocks drop). Idle it is ~68 ms, so the test still fails on
    the regressions that matter — WebP method 4 (+~120 ms) or an encode at full
    resolution (~700 ms).
    """
    frame = busy_desktop()
    frame._encode()  # first call pays for the codec's setup
    timings: list[float] = []
    for _ in range(7):
        started = time.perf_counter()
        frame._encode()
        timings.append((time.perf_counter() - started) * 1000)
    assert statistics.median(timings) < BUDGET_MS, timings


def test_the_busy_desktop_is_at_least_as_hard_as_a_real_screen() -> None:
    """The budget test proves nothing if its frame is easy: a real 3200x2000 desktop
    encodes to ~97 KiB, and more bytes out is more work in."""
    shot = busy_desktop()._encode()
    assert (shot.width, shot.height) == (1280, 800)
    assert len(shot.data) > 150 * 1024, len(shot.data)


# --------------------------------------------------------------------------- #
# Live: this machine's screen
# --------------------------------------------------------------------------- #


def test_the_primary_monitor_is_captured_at_full_physical_resolution() -> None:
    live = query_layout()
    frame = capture()
    assert frame.region == live.primary.bounds
    assert len(frame.pixels) == live.primary.bounds.width * live.primary.bounds.height * 4
    shot = frame._encode()
    assert max(shot.width, shot.height) == min(MAX_EDGE, max(frame.width, frame.height))
    assert decode(shot).size == (shot.width, shot.height)
    assert display.current_dpi_awareness() is DpiAwareness.PER_MONITOR


@pytest.mark.skipif(
    not os.environ.get(PERF_ENV),
    reason=f"set {PERF_ENV}=1 on a quiet machine to run the live capture benchmark",
)
def test_capture_and_encode_fit_the_budget() -> None:
    """`PROGRESS.md` P2-02: < 150 ms, measured on the whole primary monitor.

    A benchmark, not a gate (P2-10): it failed at 186 ms and 362 ms on a busy
    developer desktop and passed in the same session once the machine was quiet.
    """
    capture()._encode()  # first call pays for DLL loads and the DIB
    timings: list[float] = []
    for _ in range(9):
        started = time.perf_counter()
        capture()._encode()
        timings.append((time.perf_counter() - started) * 1000)
    assert statistics.median(timings) < BUDGET_MS, timings


def _test_window_rect() -> Rect:
    work = query_layout().primary.work_area
    return Rect(work.left + 160, work.top + 160, work.left + 460, work.top + 360)


def _wait_until_on_screen(window: SolidWindow) -> None:
    """DWM composes a frame or two after the window paints; allow up to two seconds."""
    rect = window.rect
    centre_x, centre_y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    centre = RegionTarget(Rect(centre_x, centre_y, centre_x + 1, centre_y + 1))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if capture(centre)._to_image().getpixel((0, 0)) == SolidWindow.RGB:
            return
        window.pump()
    pytest.fail("the red test window never appeared on screen")


@pytest.fixture
def red_window() -> Iterator[SolidWindow]:
    window = SolidWindow(_test_window_rect())
    try:
        _wait_until_on_screen(window)
        yield window
    finally:
        window.destroy()


def test_a_live_window_is_captured_where_it_is_drawn(red_window: SolidWindow) -> None:
    frame = capture(WindowTarget(red_window.hwnd))
    assert frame.region == red_window.rect
    image = frame._to_image()
    assert image.size == (red_window.rect.width, red_window.rect.height)
    for point in [(0, 0), (150, 100), (299, 199)]:
        assert image.getpixel(point) == RED, point
    assert close_to(decode(frame._encode()).getpixel((150, 100)), RED)


def test_a_live_region_is_exactly_the_pixels_asked_for(red_window: SolidWindow) -> None:
    """A region straddling the window's left edge: the edge falls on the exact pixel."""
    rect = red_window.rect
    frame = capture(RegionTarget(Rect(rect.left - 10, rect.top, rect.left + 10, rect.top + 4)))
    image = frame._to_image()
    assert image.getpixel((10, 1)) == RED
    assert image.getpixel((9, 1)) != RED


def test_a_minimised_live_window_is_refused(red_window: SolidWindow) -> None:
    red_window.minimise()
    with pytest.raises(CaptureError, match="minimised"):
        capture(WindowTarget(red_window.hwnd))


def test_a_hidden_live_window_is_refused(red_window: SolidWindow) -> None:
    red_window.hide()
    with pytest.raises(CaptureError, match="hidden"):
        capture(WindowTarget(red_window.hwnd))


def test_a_destroyed_live_window_is_refused(red_window: SolidWindow) -> None:
    hwnd = red_window.hwnd
    red_window.destroy()
    red_window.hwnd = 0
    with pytest.raises(CaptureError, match="no longer exists"):
        capture(WindowTarget(hwnd))
