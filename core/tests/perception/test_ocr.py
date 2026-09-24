"""Tests for `perception/ocr.py`, and for how `redact()` uses it.

Four layers:

* **Blind regions** on hand-built trees, each shaped like a window measured
  live: an Electron page with accessibility off (one empty `Document`), a
  Chromium window whose empty render pane is described by another branch, a
  terminal buffer named after its tab, Notepad's document holding its text.
* **Reading** — coordinates mapped from an engine's image back to the desktop —
  with a scripted engine, and the real Windows engine on text Pillow draws.
* **Redaction** with a scripted engine: every blind region is read and
  redacted, or painted black; never sent unread.
* **Live**: a real window that paints a line shaped like an API key with no UI
  tree behind it, captured off the screen, must be black in the frame and in
  the WebP.

No credential-shaped fixture is written out literally (see `test_redact.py`).
"""

from __future__ import annotations

import io
import logging
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows OCR")

from aegis_core.perception import ocr  # noqa: E402
from aegis_core.perception import redact as redact_module  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.ocr import (  # noqa: E402
    MAX_DESCRIBED_SHARE,
    OcrError,
    OcrLine,
    OcrUnavailableError,
    RawLine,
    WindowsOcr,
    blind_regions,
    read_region,
    shorten,
)
from aegis_core.perception.redact import REDACTED, Redacted, redact  # noqa: E402
from aegis_core.perception.screen import WindowTarget, capture  # noqa: E402
from aegis_core.perception.uia_tree import MAX_TEXT_CHARS, UiaElement, UiaTree, walk  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from tests.perception.helpers import CanvasWindow  # noqa: E402
from tests.perception.test_redact import (  # noqa: E402
    HWND,
    LAYOUT,
    OPENAI_KEY,
    SCREEN,
    WINDOW,
    black_pixels,
    padded,
    pixel,
    rect_pixels,
    white_frame,
    window,
)

WIDE = Rect(0, 0, 1000, 800)
WIDE_LAYOUT = DisplayLayout(
    monitors=(Monitor(device="D0", bounds=WIDE, work_area=WIDE, dpi=96, primary=True),),
    virtual=WIDE,
)


class Tree:
    """A `UiaTree` built element by element, in document order."""

    def __init__(self, root: Rect = WIDE, layout: DisplayLayout = WIDE_LAYOUT) -> None:
        self.layout = layout
        self.elements: list[UiaElement] = []
        self.root = self.add(None, "Window", "App", root)

    def add(
        self, parent: int | None, role: str, name: str, bbox: Rect | None, value: str | None = None
    ) -> int:
        index = len(self.elements)
        self.elements.append(
            UiaElement(
                id=index,
                parent=parent,
                depth=0 if parent is None else self.elements[parent].depth + 1,
                role=role,
                name=name,
                value=value,
                bbox=bbox,
                enabled=True,
                focused=False,
                offscreen=False,
                is_password=False,
                automation_id="",
                class_name="",
                runtime_id=(42, index),
            )
        )
        return index

    def change(self, index: int, **fields: object) -> None:
        self.elements[index] = replace(self.elements[index], **fields)  # type: ignore[arg-type]

    def build(self, *, truncated: bool = False, hwnd: int = HWND) -> UiaTree:
        return UiaTree(
            hwnd=hwnd,
            elements=tuple(self.elements),
            truncated=truncated,
            layout=self.layout,
            captured_at=0.0,
        )


def chrome(t: Tree) -> None:
    """A title bar with three named buttons across the top 5 % of the window."""
    bar = t.add(t.root, "TitleBar", "", Rect(0, 0, 1000, 40))
    for i, name in enumerate(("Minimise", "Maximise", "Close")):
        t.add(bar, "Button", name, Rect(850 + 50 * i, 0, 900 + 50 * i, 40))


# --------------------------------------------------------------------------- #
# Blind regions
# --------------------------------------------------------------------------- #


def test_an_empty_document_under_window_chrome_is_blind() -> None:
    """Measured in P2-03: an Electron app with accessibility off, 15 elements, all chrome."""
    t = Tree()
    chrome(t)
    page = Rect(0, 40, 1000, 800)
    t.add(t.root, "Document", "Notes - My page", page)
    assert blind_regions(t.build()) == (page,)


def test_a_window_with_nothing_in_it_is_blind_whole() -> None:
    """A canvas or a game: the window is one pane with a title."""
    assert blind_regions(Tree().build()) == (WIDE,)


def test_an_empty_pane_described_by_another_branch_is_not_blind() -> None:
    """Measured: Chromium's render pane is empty, and the page lives in a sibling branch
    over the same pixels, 40-93 % covered by small named elements."""
    t = Tree()
    t.add(t.root, "Pane", "", Rect(0, 0, 1000, 800))
    page = t.add(t.root, "Document", "Page", Rect(0, 0, 1000, 800))
    for row in range(20):
        t.add(page, "Text", f"Paragraph {row}", Rect(0, row * 40, 1000, row * 40 + 30))
    assert blind_regions(t.build()) == ()


def test_the_name_of_a_large_element_is_a_title_not_its_content() -> None:
    """Measured: a terminal's buffer is a `Text` control named after its tab."""
    t = Tree()
    chrome(t)
    buffer = Rect(0, 40, 1000, 800)
    t.add(t.root, "Text", "cmd", buffer)
    assert blind_regions(t.build()) == (buffer,)


def test_an_element_holding_its_text_as_a_value_is_not_blind() -> None:
    """Notepad's document is large and unnamed, and its value *is* the text."""
    t = Tree()
    chrome(t)
    t.add(t.root, "Document", "", Rect(0, 40, 1000, 800), value="the whole file")
    assert blind_regions(t.build()) == ()


def test_a_large_value_describes_the_window_round_it() -> None:
    """A value is content at any size, so the window holding it is not blind either."""
    t = Tree()
    t.add(t.root, "Document", "", Rect(0, 0, 1000, 800), value="the whole file")
    assert blind_regions(t.build()) == ()


def test_a_large_value_over_content_that_speaks_is_not_the_content() -> None:
    """Measured: Chromium's page `Document` holds its URL as a value over the whole
    window; the page itself is its children. An editor inside it drawn with no tree
    is still blind."""
    t = Tree()
    page = t.add(t.root, "Document", "", Rect(0, 0, 1000, 800), value="https://example.test/")
    t.add(page, "Button", "Run", Rect(0, 0, 60, 30))
    editor = Rect(0, 200, 1000, 600)
    t.add(page, "Group", "", editor)
    assert blind_regions(t.build()) == (editor,)


def test_a_small_silent_element_is_a_gap_not_a_blind_region() -> None:
    t = Tree()
    for row in range(16):
        t.add(t.root, "Text", f"Row {row}", Rect(0, row * 50, 1000, row * 50 + 40))
    t.add(t.root, "Pane", "", Rect(0, 0, 400, 300))  # 15 % of the window, and covered
    assert blind_regions(t.build()) == ()


def test_the_innermost_blind_element_is_the_region() -> None:
    t = Tree()
    chrome(t)
    host = t.add(t.root, "Custom", "", Rect(0, 40, 1000, 800))
    inner = Rect(0, 40, 1000, 780)
    t.add(host, "Text", "bash", inner)
    assert blind_regions(t.build()) == (inner,)


def test_a_region_inside_another_is_read_once() -> None:
    """Measured: two unrelated elements stacked over an editor's terminal."""
    t = Tree()
    chrome(t)
    t.add(t.root, "Group", "", Rect(0, 400, 1000, 800))
    t.add(t.root, "Group", "", Rect(0, 400, 1000, 800))
    t.add(t.root, "Group", "", Rect(100, 450, 900, 750))
    assert blind_regions(t.build()) == (Rect(0, 400, 1000, 800),)


@pytest.mark.parametrize(("covered", "blind"), [(0.2, True), (0.3, False)])
def test_coverage_decides_at_the_threshold(covered: float, blind: bool) -> None:
    """Content is many small labels, each one grid row high."""
    t = Tree()
    for row in range(round(covered * 800 / 16)):
        t.add(t.root, "Text", "Label", Rect(0, row * 16, 1000, row * 16 + 16))
    assert MAX_DESCRIBED_SHARE == 0.25
    assert bool(blind_regions(t.build())) is blind


@pytest.mark.parametrize("field", ["offscreen", "redacted"])
def test_an_offscreen_or_redacted_element_is_never_a_region(field: str) -> None:
    t = Tree()
    chrome(t)
    page = t.add(t.root, "Document", "", Rect(0, 40, 1000, 800))
    t.change(page, **{field: True})
    for row in range(20):  # so the window round it is described
        t.add(t.root, "Text", "Status", Rect(0, 40 + row * 32, 1000, 64 + row * 32))
    assert blind_regions(t.build()) == ()


def test_a_truncated_tree_has_no_blind_regions() -> None:
    """Redaction blacks all of a truncated window out already."""
    assert blind_regions(Tree().build(truncated=True)) == ()


def test_a_region_is_only_the_part_on_screen() -> None:
    t = Tree(root=Rect(500, 0, 1300, 800))  # the right half is off the only monitor
    assert blind_regions(t.build()) == (Rect(500, 0, 1000, 800),)


def test_finding_blind_regions_in_a_large_tree_is_cheap() -> None:
    t = Tree()
    for i in range(4_000):
        left, top = (i % 50) * 20, (i // 50) * 10
        t.add(t.root, "Text", "cell", Rect(left, top, left + 20, top + 9))
    tree = t.build()
    started = time.perf_counter()
    blind_regions(tree)
    assert time.perf_counter() - started < 0.25


# --------------------------------------------------------------------------- #
# Reading a region
# --------------------------------------------------------------------------- #


class Scripted:
    """An engine that answers with fixed lines and records what it was shown."""

    def __init__(self, lines: Sequence[RawLine] = (), error: Exception | None = None) -> None:
        self.lines = list(lines)
        self.error = error
        self.calls: list[tuple[bytes, int, int]] = []

    def read(self, pixels: bytes, width: int, height: int) -> Sequence[RawLine]:
        self.calls.append((pixels, width, height))
        if self.error is not None:
            raise self.error
        return self.lines


def image_bytes(region: Rect) -> bytes:
    return bytes(region.width * region.height * 4)


def test_line_boxes_come_back_in_desktop_pixels_rounded_outwards() -> None:
    region = Rect(100, 200, 500, 400)
    engine = Scripted([("Hello there", (10.4, 20.6, 90.2, 40.1))])
    (line,) = read_region(engine, image_bytes(region), region)
    assert line == OcrLine(text="Hello there", bbox=Rect(110, 220, 191, 241))
    assert engine.calls[0][1:] == (400, 200)


def test_a_line_box_is_cut_to_the_region() -> None:
    region = Rect(100, 200, 500, 400)
    engine = Scripted([("Edge", (-5.0, -5.0, 450.0, 30.0))])
    (line,) = read_region(engine, image_bytes(region), region)
    assert line.bbox == Rect(100, 200, 500, 230)


def test_blank_lines_are_dropped_and_the_rest_sorted_top_to_bottom() -> None:
    region = Rect(0, 0, 400, 400)
    engine = Scripted(
        [("Second", (0, 100, 50, 120)), ("   ", (0, 0, 50, 20)), ("First", (0, 10, 50, 30))]
    )
    assert [line.text for line in read_region(engine, image_bytes(region), region)] == [
        "First",
        "Second",
    ]


def test_lines_come_back_whole_for_the_scanner() -> None:
    """Cutting is `shorten()`'s job, after redaction has seen every character."""
    region = Rect(0, 0, 100, 100)
    long = "x" * (MAX_TEXT_CHARS + 50)
    (line,) = read_region(Scripted([(long, (0, 0, 10, 10))]), image_bytes(region), region)
    assert line.text == long
    assert len(shorten(long)) == MAX_TEXT_CHARS


def test_an_image_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="needs"):
        read_region(Scripted(), bytes(10), Rect(0, 0, 10, 10))


def test_an_engine_failure_is_an_ocr_error() -> None:
    region = Rect(0, 0, 10, 10)
    with pytest.raises(OcrError):
        read_region(Scripted(error=OcrError("boom")), image_bytes(region), region)


def test_no_screen_text_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    region = Rect(0, 0, 10, 10)
    engine = Scripted([("Transfer to account 99887766", (0, 0, 5, 5))])
    with caplog.at_level(logging.DEBUG, logger="aegis_core.perception.ocr"):
        read_region(engine, image_bytes(region), region)
    assert caplog.records
    assert all("99887766" not in repr(record.__dict__) for record in caplog.records)


# --------------------------------------------------------------------------- #
# The Windows engine
# --------------------------------------------------------------------------- #


def rendered(lines: Sequence[str], size: tuple[int, int] = (900, 200)) -> bytes:
    """Black text on white, drawn by Pillow, as the top-down BGRX a capture holds."""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    for index, line in enumerate(lines):
        draw.text((20, 20 + 70 * index), line, fill=(0, 0, 0), font=font)
    red, green, blue = image.split()
    return Image.merge("RGBX", (blue, green, red, Image.new("L", size, 0))).tobytes()


@pytest.fixture(scope="module")
def engine() -> WindowsOcr:
    return WindowsOcr()


def test_windows_ocr_reads_drawn_text(engine: WindowsOcr) -> None:
    lines = engine.read(rendered(["Invoice 4471 approved", "Ship on Friday"]), 900, 200)
    assert [text for text, _ in lines] == ["Invoice 4471 approved", "Ship on Friday"]
    (left, top, right, bottom) = lines[0][1]
    assert 0 <= left < right <= 900
    assert 0 <= top < bottom <= 100


def test_windows_ocr_reads_nothing_off_a_blank_image(engine: WindowsOcr) -> None:
    assert list(engine.read(bytes([255, 255, 255, 0]) * (300 * 100), 300, 100)) == []


def test_an_image_larger_than_the_engine_takes_is_refused(engine: WindowsOcr) -> None:
    with pytest.raises(OcrError, match="too large"):
        engine.read(bytes(4 * 20_000), 20_000, 1)


def test_windows_ocr_runs_on_a_daemon_thread(engine: WindowsOcr) -> None:
    """Invariant 14: a hung recognition must not keep the core alive."""
    engine.read(rendered(["ok"]), 900, 200)
    workers = [t for t in threading.enumerate() if t.name == "aegis-ocr"]
    assert workers
    assert all(worker.daemon for worker in workers)


def test_a_recognition_that_hangs_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(*_args: object) -> list[RawLine]:
        await __import__("asyncio").sleep(30)
        return []

    monkeypatch.setattr(ocr, "_recognise", hang)
    slow = WindowsOcr(timeout_s=0.3)
    started = time.monotonic()
    with pytest.raises(OcrError, match="took over"):
        slow.read(rendered(["x"]), 900, 200)
    assert time.monotonic() - started < 0.3 + ocr.GRACE_S


def test_an_unexpected_failure_is_an_ocr_error_and_the_thread_lives_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def flaky(*_args: object) -> list[RawLine]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("provider text that must not leak")
        return [("fine", (0.0, 0.0, 1.0, 1.0))]

    monkeypatch.setattr(ocr, "_recognise", flaky)
    engine = WindowsOcr()
    with pytest.raises(OcrError) as caught:
        engine.read(rendered(["x"]), 900, 200)
    assert "provider text" not in str(caught.value)
    assert list(engine.read(rendered(["x"]), 900, 200)) == [("fine", (0.0, 0.0, 1.0, 1.0))]


def test_no_ocr_language_is_a_message_a_person_can_act_on_and_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[int] = []

    def none_installed() -> object:
        created.append(1)
        raise OcrUnavailableError("Windows has no text-recognition language installed.")

    monkeypatch.setattr(ocr, "_create_engine", none_installed)
    engine = WindowsOcr()
    for _ in range(2):
        with pytest.raises(OcrUnavailableError, match="language"):
            engine.read(rendered(["x"]), 900, 200)
    assert len(created) == 2


# --------------------------------------------------------------------------- #
# Redaction: read and redacted, or painted black
# --------------------------------------------------------------------------- #

#: A window whose tree describes only a title bar: everything under it is blind.
TITLE = Rect(50, 50, 350, 70)
PAGE = Rect(50, 70, 350, 250)


def blind_tree() -> UiaTree:
    t = Tree(root=WINDOW, layout=LAYOUT)
    t.add(t.root, "TitleBar", "", TITLE)
    t.add(1, "Button", "Close", Rect(320, 50, 350, 70))
    t.add(t.root, "Document", "", PAGE)
    return t.build()


def redacted(engine: Scripted | None, tree: UiaTree | None = None) -> Redacted:
    return redact(white_frame(), [tree or blind_tree()], stacking=[window()], ocr=engine)


def test_blind_regions_are_what_ocr_is_shown() -> None:
    engine = Scripted()
    redacted(engine)
    ((pixels, width, height),) = engine.calls
    assert (width, height) == (PAGE.width, PAGE.height)
    assert len(pixels) == PAGE.width * PAGE.height * 4


def test_ocr_text_comes_back_on_the_observation() -> None:
    engine = Scripted([("Quarterly report", (10, 10, 120, 30))])
    result = redacted(engine)
    assert result.text == (OcrLine(text="Quarterly report", bbox=Rect(60, 80, 170, 100)),)
    assert result.unread == ()
    assert black_pixels(result.frame) == rect_pixels(SCREEN) - rect_pixels(WINDOW)


def test_a_credential_read_by_ocr_is_black_in_the_pixels_and_gone_from_the_text() -> None:
    engine = Scripted([(f"Token {OPENAI_KEY} expires", (10, 40, 280, 60))])
    result = redacted(engine)
    (line,) = result.text
    assert line.redacted
    assert line.text == f"Token {REDACTED} expires"
    box = Rect(60, 110, 330, 130)
    assert rect_pixels(padded(box)) & rect_pixels(WINDOW) <= black_pixels(result.frame)
    assert OPENAI_KEY not in repr(result)


def test_a_line_with_a_secret_label_loses_all_of_its_text() -> None:
    """The value may be anywhere on the line: after the label, or before it."""
    engine = Scripted([("Recovery phrase: apple river stone", (10, 40, 280, 60))])
    (line,) = redacted(engine).text
    assert (line.text, line.redacted) == (REDACTED, True)


def test_a_clean_line_leaves_its_pixels_alone() -> None:
    engine = Scripted([("Quarterly report ready", (10, 40, 200, 60))])
    result = redacted(engine)
    assert pixel(result.frame, 100, 120) == (255, 255, 255)


@pytest.mark.parametrize(
    "error", [OcrError("slow"), OcrUnavailableError("no language")], ids=["failed", "none"]
)
def test_a_region_ocr_cannot_read_is_painted_black_and_reported(
    error: OcrError, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aegis_core.perception.redact"):
        result = redacted(Scripted(error=error))
    assert result.unread == (PAGE,)
    assert rect_pixels(PAGE) <= black_pixels(result.frame)
    assert result.text == ()
    assert any(record.msg == "screen.ocr_unread" for record in caplog.records)


def test_a_caller_that_opts_out_of_ocr_gets_the_region_black() -> None:
    result = redacted(None)
    assert result.unread == (PAGE,)
    assert rect_pixels(PAGE) <= black_pixels(result.frame)


def test_ocr_is_not_run_on_a_window_its_tree_describes() -> None:
    t = Tree(root=WINDOW, layout=LAYOUT)
    for row in range(10):
        t.add(t.root, "Text", f"Row {row}", Rect(50, 50 + row * 20, 350, 68 + row * 20))
    engine = Scripted()
    result = redacted(engine, t.build())
    assert engine.calls == []
    assert result.unread == ()


def test_ocr_sees_the_painted_frame_never_a_secret_field() -> None:
    """A password box inside a blind region is already black when OCR looks."""
    t = Tree(root=WINDOW, layout=LAYOUT)
    t.add(t.root, "TitleBar", "", TITLE)
    page = t.add(t.root, "Document", "", PAGE)
    field = Rect(100, 150, 200, 170)
    t.add(page, "Edit", "Password", field)
    t.change(3, is_password=True)
    engine = Scripted()
    redacted(engine, t.build())
    ((pixels, width, _),) = engine.calls
    x, y = 150 - PAGE.left, 160 - PAGE.top
    assert pixels[(y * width + x) * 4 : (y * width + x) * 4 + 3] == b"\0\0\0"


def test_every_line_is_scanned_before_the_cap_drops_any(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact_module, "MAX_LINES", 2)
    engine = Scripted(
        [
            ("one", (10, 0, 50, 10)),
            ("two", (10, 20, 50, 30)),
            (f"three {OPENAI_KEY}", (10, 100, 280, 120)),
        ]
    )
    result = redacted(engine)
    assert [line.text for line in result.text] == ["one", "two"]
    assert rect_pixels(Rect(60, 170, 330, 190)) <= black_pixels(result.frame)


def test_a_credential_past_the_text_cap_is_still_found() -> None:
    long = "x " * MAX_TEXT_CHARS + OPENAI_KEY
    (line,) = redacted(Scripted([(long, (10, 40, 280, 60))])).text
    assert line.redacted
    assert OPENAI_KEY not in line.text
    assert len(line.text) <= MAX_TEXT_CHARS


def test_no_ocr_text_reaches_the_redaction_log(caplog: pytest.LogCaptureFixture) -> None:
    engine = Scripted([("Wire 4000 to account 99887766", (10, 40, 200, 60))])
    with caplog.at_level(logging.DEBUG, logger="aegis_core.perception.redact"):
        redacted(engine)
    assert caplog.records
    assert all("99887766" not in repr(record.__dict__) for record in caplog.records)


# --------------------------------------------------------------------------- #
# Live: a key painted with no tree behind it, off the real screen
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module", autouse=True)
def _per_monitor_aware() -> None:
    ensure_dpi_awareness()


def test_golden_a_painted_key_is_black_off_the_real_screen() -> None:
    work = query_layout().primary.work_area
    rect = Rect(work.left + 200, work.top + 200, work.left + 1400, work.top + 440)
    lines = ("Account settings", f"Token {OPENAI_KEY}", "Invoice 4471 approved")
    with CanvasWindow(rect, lines) as canvas:
        tree = walk(canvas.hwnd)
        assert len(tree.elements) == 1  # UI Automation knows nothing of the text
        frame = capture(WindowTarget(canvas.hwnd))
        before = frame.pixels[:]
        result = redact(frame, [tree])
        key_band = canvas.line_rect(1)
    assert [line.text for line in result.text] == [
        "Account settings",
        f"Token {REDACTED}",
        "Invoice 4471 approved",
    ]
    assert result.unread == ()
    secret = next(line for line in result.text if line.redacted)
    assert key_band.contains_rect(secret.bbox)
    # Black in the bytes: every pixel of the line, where there was ink before.
    assert rect_pixels(secret.bbox) <= black_pixels(result.frame)
    assert before != result.frame.pixels
    # And in the WebP a model would be sent.
    shot = result.encode()
    image = Image.open(io.BytesIO(shot.data)).convert("RGB")
    scale = shot.width / frame.width
    for x in range(secret.bbox.left, secret.bbox.right, 25):
        y = (secret.bbox.top + secret.bbox.bottom) // 2
        at = (int((x - frame.region.left) * scale), int((y - frame.region.top) * scale))
        value = image.getpixel(at)
        assert isinstance(value, tuple)
        assert max(value) <= 8
