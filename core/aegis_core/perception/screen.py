"""Screen capture: a monitor, a window or a region, as pixels and as WebP.

`capture()` copies a rectangle of the virtual desktop into a `Frame` — the raw
pixels plus the region they came from and the `DisplayLayout` they were measured
under. A `Screenshot` — what a model is shown, at most `MAX_EDGE` pixels on its long
side (`ARCHITECTURE.md § 6.2`), WebP — is made from a frame **only** through
`perception/redact.py`, never directly.

Three rules shape it:

* **The DPI check comes before `mss`.** Constructing an `mss` object calls
  `SetProcessDpiAwareness` as a side effect. In a process `ensure_dpi_awareness()`
  already made per-monitor aware, that call is refused and harmless; in one it
  did not, it would quietly pick an awareness for the whole process. So
  `query_layout()` — which refuses a thread that is not per-monitor aware — runs
  first, and `mss` is never constructed on a thread that would be given scaled
  coordinates.
* **A frame is only as good as the layout it was taken under.** The layout is
  re-verified after the grab, so a monitor unplugged or rescaled mid-capture is a
  `LayoutChangedError`, never a frame whose region points at the wrong pixels
  (`RECOVERY.md § 3.3`).
* **A window is captured as it appears on screen.** Its region is copied from the
  desktop, so anything on top of it is in the frame too. That is deliberate: a click
  lands on whatever is on top, and the model must see what the click will hit. A
  window with nothing on screen — minimised, hidden, on another virtual desktop — is
  refused with a message that says which.

Nothing here redacts, so nothing here is public that turns a frame into something
that could leave the process. `Frame._to_image()` and `Frame._encode()` are private
to this module and `redact.py`, which black-boxes password fields and every pixel
no UI tree vouches for first (invariant 8). `tests/perception/test_redact.py`
fails if anything else in the core calls them.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Final, Literal, cast

import mss
from PIL import Image

from aegis_core.perception import win32
from aegis_core.perception.display import (
    DisplayLayout,
    OffScreenError,
    Rect,
    query_layout,
    verify_layout,
)

log = logging.getLogger(__name__)

_screen_lock = threading.Lock()
_screen: mss.MSS | None = None

#: The long edge of any image a model is shown (`ARCHITECTURE.md § 6.2`). Also the
#: largest `max_edge` `Frame._encode()` accepts, so no caller can ask for more.
MAX_EDGE: Final = 1280

#: Lossy WebP at 80 keeps UI text legible at `MAX_EDGE` for 60-110 KiB a frame.
WEBP_QUALITY: Final = 80

#: libwebp's fastest method. Measured at 1280x800: method 0 encodes in ~33 ms,
#: method 4 (Pillow's default) in ~150 ms — the whole capture budget on its own —
#: for files about 15 % smaller.
WEBP_METHOD: Final = 0

#: Area averaging: each output pixel is the mean of the screen pixels it covers. It
#: is the right filter for shrinking UI text — no ringing round the glyphs — and the
#: fastest. Measured on a 3200x2000 frame: ~20 ms, against ~34 ms for Lanczos after
#: an integer reduce and ~73 ms for Lanczos alone.
RESAMPLE: Final = Image.Resampling.BOX

#: `REVIEW.md § 2`: no unbounded buffer. One 8K monitor (7680x4320, 127 MiB at four
#: bytes a pixel) is the largest single capture. A bigger request is a bug, not a desk.
MAX_CAPTURE_PIXELS: Final = 7680 * 4320

#: Four bytes per pixel, in the order Windows' GDI writes them.
BYTES_PER_PIXEL: Final = 4

WEBP_MEDIA_TYPE: Final = "image/webp"


class CaptureError(RuntimeError):
    """The screen could not be captured, for a reason the message states."""


@dataclass(frozen=True, slots=True)
class MonitorTarget:
    """A whole monitor, by GDI device name. `None` is the primary monitor."""

    device: str | None = None


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """The on-screen area of a top-level window."""

    hwnd: int


@dataclass(frozen=True, slots=True)
class RegionTarget:
    """A rectangle of the virtual desktop, in physical pixels."""

    rect: Rect


CaptureTarget = MonitorTarget | WindowTarget | RegionTarget


@dataclass(frozen=True, slots=True, eq=False)
class Frame:
    """One capture at full resolution: the pixels, where they came from, and when.

    `pixels` is top-down **BGRX** — GDI's order, four bytes a pixel, the fourth
    undefined — and is kept as `mss` returned it, because converting 25 MiB costs
    more than the whole encode. Nothing may mutate it. It is not in `repr`: it is
    the user's screen.
    """

    region: Rect
    layout: DisplayLayout
    captured_at: float
    pixels: bytearray = field(repr=False)

    def __post_init__(self) -> None:
        if self.region.is_empty:
            raise CaptureError("A frame cannot be empty.")
        expected = self.region.width * self.region.height * BYTES_PER_PIXEL
        if len(self.pixels) != expected:
            raise CaptureError(f"A frame of {self.region} needs {expected} bytes.")

    @property
    def width(self) -> int:
        return self.region.width

    @property
    def height(self) -> int:
        return self.region.height

    def _to_image(self, max_edge: int = MAX_EDGE) -> Image.Image:
        """The frame as an RGB image no longer than `max_edge` on its long side.

        Never upscales. The resize runs on a zero-copy view that *calls* the BGRX
        bytes RGBX — resampling treats each channel alike, so the order does not
        matter yet — and blue and red are swapped only on the small result, which
        is a twentieth of the work of swapping first.
        """
        _check_max_edge(max_edge)
        size = fit_within(self.width, self.height, max_edge)
        # Pillow reads any buffer; its stubs only name `bytes`, and a copy to satisfy
        # them would cost ~15 ms of a 150 ms budget.
        pixels = cast("bytes", self.pixels)
        view = Image.frombuffer("RGBX", (self.width, self.height), pixels, "raw", "RGBX", 0, 1)
        if size != view.size:
            view = view.resize(size, RESAMPLE)
        blue, green, red, _ = view.split()
        return Image.merge("RGB", (red, green, blue))

    def _encode(self, *, max_edge: int = MAX_EDGE, quality: int = WEBP_QUALITY) -> Screenshot:
        """The frame as WebP. Only `redact.py` may call this, on a frame it has redacted."""
        if not 1 <= quality <= 100:
            raise ValueError(f"WebP quality must be 1-100, not {quality}.")
        image = self._to_image(max_edge)
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=quality, method=WEBP_METHOD)
        return Screenshot(
            data=buffer.getvalue(),
            width=image.width,
            height=image.height,
            region=self.region,
            layout_fingerprint=self.layout.fingerprint,
            captured_at=self.captured_at,
        )


@dataclass(frozen=True, slots=True)
class Screenshot:
    """An encoded frame. `region` is the physical rectangle its pixels cover.

    Image pixel `(x, y)` shows physical pixels starting at
    `region.left + x * region.width / width` — the scale is kept implicit so that
    no rounded ratio can drift from the numbers it was derived from.
    """

    data: bytes = field(repr=False)
    width: int
    height: int
    region: Rect
    layout_fingerprint: str
    captured_at: float
    media_type: Literal["image/webp"] = WEBP_MEDIA_TYPE


def fit_within(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """`(width, height)` scaled down so the long side is at most `max_edge`.

    Integer arithmetic, rounding half up, never below one pixel and never larger
    than the input.
    """
    _check_max_edge(max_edge)
    if width <= 0 or height <= 0:
        raise ValueError(f"Cannot fit a {width}x{height} image.")
    long_side = max(width, height)
    if long_side <= max_edge:
        return width, height

    def scaled(side: int) -> int:
        return max(1, (2 * side * max_edge + long_side) // (2 * long_side))

    return scaled(width), scaled(height)


def _check_max_edge(max_edge: int) -> None:
    if not 1 <= max_edge <= MAX_EDGE:
        raise ValueError(f"max_edge must be 1-{MAX_EDGE}, not {max_edge}.")


def capture(target: CaptureTarget | None = None) -> Frame:
    """Copy `target` (the primary monitor by default) off the screen, right now.

    Raises `DpiAwarenessError` on a thread that is not per-monitor aware,
    `OffScreenError` for a region or window that is on no monitor,
    `LayoutChangedError` if the monitors changed during the capture, and
    `CaptureError` for everything else — each with a message a person can act on.
    """
    started = time.perf_counter()
    layout = query_layout()
    chosen = target if target is not None else MonitorTarget()
    region = _resolve(chosen, layout)
    captured_at = time.time()
    pixels = _grab(region)
    verify_layout(layout)
    frame = Frame(region=region, layout=layout, captured_at=captured_at, pixels=pixels)
    log.debug(
        "screen.captured",
        extra={
            "target": type(chosen).__name__,
            "width": region.width,
            "height": region.height,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return frame


def _resolve(target: CaptureTarget, layout: DisplayLayout) -> Rect:
    """The physical rectangle `target` covers, checked against `layout`."""
    if isinstance(target, MonitorTarget):
        region = _monitor_region(target.device, layout)
    elif isinstance(target, WindowTarget):
        region = _window_region(target.hwnd, layout)
    else:
        region = target.rect
    return _checked(region, layout)


def _monitor_region(device: str | None, layout: DisplayLayout) -> Rect:
    if device is None:
        return layout.primary.bounds
    for monitor in layout.monitors:
        if monitor.device == device:
            return monitor.bounds
    raise CaptureError(f"No monitor called {device} is connected.")


def _window_region(hwnd: int, layout: DisplayLayout) -> Rect:
    """The part of the window that is on the desktop. Refuses one with none."""
    if not win32.is_window(hwnd):
        raise CaptureError("That window no longer exists.")
    if win32.is_minimised(hwnd):
        raise CaptureError("That window is minimised. Restore it before capturing it.")
    if not win32.is_window_visible(hwnd) or win32.is_cloaked(hwnd):
        raise CaptureError(
            "That window is hidden or on another virtual desktop, so none of it is on screen."
        )
    bounds = win32.window_bounds(hwnd)
    if bounds is None:
        raise CaptureError("Windows would not say where that window is.")
    region = _intersection(
        Rect(int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom)),
        layout.virtual,
    )
    if region.is_empty:
        raise OffScreenError("That window is entirely off the screen.")
    return region


def _checked(region: Rect, layout: DisplayLayout) -> Rect:
    """Refuse a region that is empty, off the desktop, on no monitor, or too large.

    A region may cross a dead zone between monitors; those pixels come out black.
    One that touches no monitor at all shows nothing and is refused.
    """
    if region.is_empty:
        raise CaptureError(f"The region {region} is empty.")
    if not layout.virtual.contains_rect(region):
        raise OffScreenError(f"The region {region} extends past the edge of the desktop.")
    if all(_intersection(region, m.bounds).is_empty for m in layout.monitors):
        raise OffScreenError(f"The region {region} is not on any monitor.")
    if region.width * region.height > MAX_CAPTURE_PIXELS:
        raise CaptureError(
            f"The region {region} is larger than the {MAX_CAPTURE_PIXELS}-pixel capture limit."
        )
    return region


def _intersection(a: Rect, b: Rect) -> Rect:
    return Rect(
        max(a.left, b.left), max(a.top, b.top), min(a.right, b.right), min(a.bottom, b.bottom)
    )


def _shared_screen() -> mss.MSS:
    """The process' one `mss` object, created on first use — never before `query_layout()`.

    Kept rather than built per call because each one allocates a fresh DIB section
    for the grab, and re-faulting 25 MiB of new memory costs ~20 ms a capture. It is
    safe to share: `mss` serialises its own calls under a lock, and a screen DC may
    be used from any thread so long as one thread uses it at a time.
    """
    global _screen
    with _screen_lock:
        if _screen is None:
            _screen = mss.MSS()
        return _screen


def _discard_screen() -> None:
    """Drop the shared object after a failure, so the next capture starts from fresh DCs.

    A DC taken before a lock, a session switch or a driver reset can stay broken
    after it; rebuilding costs one allocation, and keeping it would fail forever.
    """
    global _screen
    with _screen_lock:
        broken, _screen = _screen, None
    if broken is not None:
        try:
            broken.close()
        except (mss.ScreenShotError, OSError):
            log.warning("screen.close_failed")


def _grab(region: Rect) -> bytearray:
    """The pixels of `region`, as BGRX."""
    box = {"left": region.left, "top": region.top, "width": region.width, "height": region.height}
    try:
        shot = _shared_screen().grab(box)
    except (mss.ScreenShotError, OSError) as error:
        _discard_screen()
        raise CaptureError(
            "Windows refused to copy the screen. This happens while the machine is locked "
            "or a secure desktop, such as a UAC prompt, is showing."
        ) from error
    if (shot.width, shot.height) != (region.width, region.height):
        raise CaptureError(f"Asked for {region.width}x{region.height} and got {shot.size}.")
    return shot.raw
