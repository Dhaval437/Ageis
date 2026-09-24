"""OCR: text read off the pixels of the parts of a window its UI tree cannot describe.

The UI tree comes first (`ARCHITECTURE.md § 6.3`), and for most windows it is
enough. Some windows draw their content themselves and tell UI Automation
nothing about it: a canvas, a game, a terminal buffer, and — measured in `P2-03`
— an Electron app that has not turned accessibility on, whose whole page is one
empty `Document`. `blind_regions()` finds those parts of a walked window, and
`read_region()` reads their text with the **Windows OCR engine**
(`Windows.Media.Ocr`, through the `winrt` projection) — built into Windows, run
on this machine, nothing sent anywhere.

What calls this, and why it matters where: **`redact()` does, and nothing
else.** OCR reads pixels, and pixels only leave the process through redaction
(invariant 8), so OCR runs on the frame *after* redaction has blacked out every
secret field and every pixel no tree vouches for — it cannot read a password
box or another app's window — and the lines it returns go through the same
credential and secret-label rules as tree text before any of it can reach a
model. This module never sees a `Frame`: it is handed plain bytes by
`redact.py`, which the source scan in `test_redact.py` already allows to read
them. A blind region is therefore either *read and redacted* or *painted
black*; it is never sent unread.

The engine runs on one daemon thread of its own: WinRT's recognition is
asynchronous, the rest of perception is not, and a caller may already have an
event loop running. Every call is bounded (`TIMEOUT_S`), and the thread is a
daemon so a hung recognition can never keep the core alive past its UI
(invariant 14).

Every line of text here is untrusted screen text. None of it is logged: the
debug line carries counts and timings.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine as WinRtOcrEngine
from winrt.windows.storage.streams import Buffer

from aegis_core.perception.display import Rect
from aegis_core.perception.prune import visible_part
from aegis_core.perception.uia_tree import ELLIPSIS, MAX_TEXT_CHARS, UiaTree

log = logging.getLogger(__name__)

#: An element smaller than this share of its window is a gap in a layout, not a
#: surface the tree could be hiding content in — and a *name* on an element at
#: least this large is a title, not a description of its pixels (a terminal's
#: buffer is named after its tab).
MIN_BLIND_SHARE: Final = 0.2

#: A large element is described when elements that describe pixels — holding a
#: value, or small and named — cover at least this share of it. Measured: an Electron window's empty
#: render pane, whose content sits in another branch of the tree, is 40-93 %
#: covered; a page drawn with accessibility off is ~0 %.
MAX_DESCRIBED_SHARE: Final = 0.25

#: Physical pixels per side of the grid coverage is counted on.
CELL_PX: Final = 16

#: Seconds one recognition may take. Measured: a whole 3200x2000 monitor reads in
#: ~330 ms, a 500x300 window in ~90 ms.
TIMEOUT_S: Final = 3.0

#: Extra seconds the caller waits for the OCR thread to report its own timeout
#: before giving up on the thread itself.
GRACE_S: Final = 1.0

#: `REVIEW.md § 2`: bounded. At most this many lines of one observation are kept for
#: a model; more is not a page it reads line by line.
MAX_LINES: Final = 2_000

BYTES_PER_PIXEL: Final = 4


class OcrError(RuntimeError):
    """The text of a region could not be read, for the reason the message states."""


class OcrUnavailableError(OcrError):
    """This machine has no OCR engine the core can use."""


@dataclass(frozen=True, slots=True)
class OcrLine:
    """One line of text read off the screen, and where it is.

    `bbox` is physical virtual-desktop pixels, the smallest rectangle round the
    line's words. `text` is screen text and kept out of `repr`. `redacted` is set
    by `redact.py` when it blanked the line, exactly as on a `UiaElement`.
    """

    text: str = field(repr=False)
    bbox: Rect
    redacted: bool = False


#: A line as an engine reports it: its text and `(left, top, right, bottom)` in the
#: pixels of the image it was given.
RawLine = tuple[str, tuple[float, float, float, float]]


class OcrEngine(Protocol):
    """Something that reads the lines of text in a top-down BGRX image."""

    def read(self, pixels: bytes, width: int, height: int) -> Sequence[RawLine]: ...


def blind_regions(tree: UiaTree) -> tuple[Rect, ...]:
    """The parts of `tree`'s window whose content the tree does not describe.

    An element is blind when its visible part covers at least `MIN_BLIND_SHARE`
    of the window, it holds no value, and the elements describing pixels
    anywhere in the tree — not only inside it: a Chromium page lives in a
    different branch from the pane that shows it — cover less than
    `MAX_DESCRIBED_SHARE` of it.

    What describes pixels: a small element with a name or a value, and a large
    element whose value is its content — one with nothing inside it that speaks,
    as Notepad's document holds its text. A large element's *name* is a title
    (a terminal buffer is named after its tab), and a large value with speaking
    elements inside is not the content either: measured, Chromium's page
    `Document` carries its URL as a value over the whole window.

    The regions are the innermost blind elements: the terminal buffer rather
    than the window round it. A truncated tree has none, since redaction blacks
    all of its window out anyway.
    """
    elements = tree.elements
    if tree.truncated or not elements or elements[0].bbox is None:
        return ()
    window = elements[0].bbox
    large = MIN_BLIND_SHARE * window.width * window.height
    visible = [None if e.offscreen else visible_part(e.bbox, window, tree.layout) for e in elements]
    # Document order puts every descendant after its ancestor, so one reverse
    # pass tells each element whether anything inside it speaks.
    speaks_inside = [False] * len(elements)
    for e in reversed(elements):
        if e.parent is not None and (speaks_inside[e.id] or e.name or e.value):
            speaks_inside[e.parent] = True
    grid = _Grid(window)
    for e, part in zip(elements, visible, strict=True):
        if part is None or not (e.name or e.value):
            continue
        if _area(part) < large or (e.value and not speaks_inside[e.id]):
            grid.mark(part)
    blind: set[int] = set()
    for e, part in zip(elements, visible, strict=True):
        if part is None or _area(part) < large or e.value or e.redacted:
            continue
        if grid.share(part) < MAX_DESCRIBED_SHARE:
            blind.add(e.id)
    # Keep the innermost: drop every blind element with a blind element inside it.
    outer = {parent for e in elements if e.id in blind for parent in _ancestors(e.id, tree)}
    innermost = [
        part
        for e, part in zip(elements, visible, strict=True)
        if e.id in blind and e.id not in outer and part is not None
    ]
    # Providers stack unrelated elements over the same pixels (measured: two over an
    # editor's terminal). Reading a region inside another would read it twice.
    return tuple(
        part
        for i, part in enumerate(innermost)
        if not any(
            other.contains_rect(part) and (other != part or j < i)
            for j, other in enumerate(innermost)
            if j != i
        )
    )


def read_region(engine: OcrEngine, pixels: bytes, region: Rect) -> tuple[OcrLine, ...]:
    """The lines of text in `pixels`, the top-down BGRX image of `region`.

    Line boxes come back in physical virtual-desktop pixels, cut to `region`,
    top to bottom. **Every** line is returned, **whole** — the caller scans them
    for secrets before it keeps `MAX_LINES` of them and cuts each with
    `shorten()`, so no cap can leave text on screen that was never scanned.
    Raises `OcrError` if the engine fails or takes too long.
    """
    if region.is_empty:
        raise ValueError("An OCR region cannot be empty.")
    if len(pixels) != region.width * region.height * BYTES_PER_PIXEL:
        raise ValueError(f"An image of {region} needs {region.width * region.height * 4} bytes.")
    started = time.perf_counter()
    lines: list[OcrLine] = []
    for text, (left, top, right, bottom) in engine.read(pixels, region.width, region.height):
        if not text.strip():
            continue
        box = Rect(
            max(region.left + math.floor(left), region.left),
            max(region.top + math.floor(top), region.top),
            min(region.left + math.ceil(right), region.right),
            min(region.top + math.ceil(bottom), region.bottom),
        )
        if box.is_empty:
            continue
        lines.append(OcrLine(text=text, bbox=box))
    lines.sort(key=lambda line: (line.bbox.top, line.bbox.left))
    log.debug(
        "screen.ocr_read",
        extra={
            "pixels": region.width * region.height,
            "lines": len(lines),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return tuple(lines)


def _ancestors(index: int, tree: UiaTree) -> list[int]:
    found: list[int] = []
    parent = tree.elements[index].parent
    while parent is not None:
        found.append(parent)
        parent = tree.elements[parent].parent
    return found


def _area(rect: Rect) -> int:
    return rect.width * rect.height


class _Grid:
    """The window in `CELL_PX` squares, each marked once something describes it."""

    def __init__(self, window: Rect) -> None:
        self._origin = window
        self._columns = -(-window.width // CELL_PX)
        self._rows = -(-window.height // CELL_PX)
        self._cells = bytearray(self._columns * self._rows)

    def _span(self, rect: Rect) -> tuple[range, int, int]:
        left = (rect.left - self._origin.left) // CELL_PX
        right = -(-(rect.right - self._origin.left) // CELL_PX)
        top = (rect.top - self._origin.top) // CELL_PX
        bottom = -(-(rect.bottom - self._origin.top) // CELL_PX)
        return range(max(top, 0), min(bottom, self._rows)), max(left, 0), min(right, self._columns)

    def mark(self, rect: Rect) -> None:
        rows, left, right = self._span(rect)
        if right <= left:
            return
        fill = b"" * (right - left)
        for row in rows:
            start = row * self._columns
            self._cells[start + left : start + right] = fill

    def share(self, rect: Rect) -> float:
        rows, left, right = self._span(rect)
        total = len(rows) * max(right - left, 0)
        if total == 0:
            return 0.0
        marked = sum(
            self._cells[row * self._columns + left : row * self._columns + right].count(1)
            for row in rows
        )
        return marked / total


def shorten(text: str) -> str:
    """`text` cut to `MAX_TEXT_CHARS`, as a walked name is. Only after it was scanned."""
    if len(text) > MAX_TEXT_CHARS:
        return text[: MAX_TEXT_CHARS - len(ELLIPSIS)] + ELLIPSIS
    return text


# --------------------------------------------------------------------------- #
# The Windows OCR engine
# --------------------------------------------------------------------------- #

_Job = tuple[bytes, int, int, "concurrent.futures.Future[list[RawLine]]"]


class WindowsOcr:
    """`Windows.Media.Ocr` in the languages of the user's profile, on its own thread.

    The engine is created on first use, and again on any call after one found no
    OCR language, so installing one takes effect without a restart.
    """

    def __init__(self, *, timeout_s: float = TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._jobs: queue.Queue[_Job] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def read(self, pixels: bytes, width: int, height: int) -> Sequence[RawLine]:
        result: concurrent.futures.Future[list[RawLine]] = concurrent.futures.Future()
        self._start()
        self._jobs.put((pixels, width, height, result))
        try:
            return result.result(timeout=self._timeout_s + GRACE_S)
        except concurrent.futures.TimeoutError:
            result.cancel()
            raise OcrError(
                f"Reading text off the screen took over {self._timeout_s:g} s."
            ) from None

    def _start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="aegis-ocr", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        engine: WinRtOcrEngine | None = None
        while True:
            pixels, width, height, result = self._jobs.get()
            if not result.set_running_or_notify_cancel():
                continue
            try:
                if engine is None:
                    engine = _create_engine()
                task = asyncio.wait_for(_recognise(engine, pixels, width, height), self._timeout_s)
                result.set_result(loop.run_until_complete(task))
            except TimeoutError:
                result.set_exception(
                    OcrError(f"Reading text off the screen took over {self._timeout_s:g} s.")
                )
            except OcrError as error:
                result.set_exception(error)
            except OSError as error:  # a failed WinRT call surfaces as an OSError
                engine = None
                result.set_exception(OcrError(f"Windows OCR failed (0x{_hresult(error):08X})."))
            except Exception as error:  # the thread must outlive any one bad call
                engine = None
                log.error("screen.ocr_failed", extra={"error": type(error).__name__})
                result.set_exception(OcrError("Windows OCR failed unexpectedly."))


def _create_engine() -> WinRtOcrEngine:
    engine = WinRtOcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrUnavailableError(
            "Windows has no text-recognition language installed. Add one under "
            "Settings > Time & language > Language & region."
        )
    return engine


async def _recognise(
    engine: WinRtOcrEngine, pixels: bytes, width: int, height: int
) -> list[RawLine]:
    limit = WinRtOcrEngine.max_image_dimension
    if width > limit or height > limit:
        raise OcrError(f"An area over {limit} pixels on a side is too large to read.")
    buffer = Buffer(len(pixels))
    buffer.length = len(pixels)
    memoryview(buffer)[:] = pixels
    # The fourth byte of a GDI capture is undefined, so the bitmap ignores it.
    bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
        buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE
    )
    result = await engine.recognize_async(bitmap)
    lines: list[RawLine] = []
    for line in result.lines:
        rects = [word.bounding_rect for word in line.words]
        if not rects:
            continue
        lines.append(
            (
                line.text,
                (
                    min(r.x for r in rects),
                    min(r.y for r in rects),
                    max(r.x + r.width for r in rects),
                    max(r.y + r.height for r in rects),
                ),
            )
        )
    return lines


def _hresult(error: OSError) -> int:
    return (getattr(error, "winerror", None) or error.errno or 0) & 0xFFFFFFFF


#: The engine `redact()` uses unless told otherwise.
SYSTEM_OCR: Final = WindowsOcr()
