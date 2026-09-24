"""Tests for the text-line half of `perception/grounding.py` (`P2-13`).

A line of OCR text has no element to re-find, so what grounds it is reading it
again: the window is observed afresh — walked, captured, redacted, OCR'd — and
the line must be there, the same text, in the same place. The fakes here replace
only the operating system (the walk, the capture, the window stack, the hit
test); the re-read itself runs through the real `redact()`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

from aegis_core.perception import grounding  # noqa: E402
from aegis_core.perception import redact as redact_module  # noqa: E402
from aegis_core.perception import win32 as display_win32  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    LayoutChangedError,
    Monitor,
    Point,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.grounding import (  # noqa: E402
    MIN_TEXT_PX,
    OBSERVE_AGAIN,
    TEXT_PLACE_PX,
    GroundingError,
    TextTarget,
    resolve_text,
    text_targets,
)
from aegis_core.perception.ocr import SYSTEM_OCR, OcrError, OcrLine, RawLine  # noqa: E402
from aegis_core.perception.redact import Redacted, redact  # noqa: E402
from aegis_core.perception.screen import CaptureTarget, Frame, WindowTarget, capture  # noqa: E402
from aegis_core.perception.uia_tree import UiaTree, walk  # noqa: E402

from tests.perception.helpers import CanvasWindow  # noqa: E402
from tests.perception.test_ocr import PAGE, Scripted, blind_tree  # noqa: E402
from tests.perception.test_redact import (  # noqa: E402
    HWND,
    LAYOUT,
    OPENAI_KEY,
    SCREEN,
    white_frame,
    window,
)

#: Where lines sit, in `PAGE`'s own pixels, and where that puts them on the desktop.
INVOICES = ("Invoices", (10.0, 10.0, 120.0, 30.0))
INVOICES_BOX = Rect(60, 80, 170, 100)
REPORTS = ("Reports", (10.0, 50.0, 100.0, 70.0))
REPORTS_BOX = Rect(60, 120, 150, 140)


def shifted(line: RawLine, dx: float = 0, dy: float = 0) -> RawLine:
    text, (left, top, right, bottom) = line
    return text, (left + dx, top + dy, right + dx, bottom + dy)


def observed(lines: Sequence[RawLine] = (INVOICES, REPORTS)) -> Redacted:
    """What the model was shown: a window with one blind page, read by OCR."""
    return redact(white_frame(), [blind_tree()], stacking=[window()], ocr=Scripted(lines))


class Screen:
    """The machine as `resolve_text` sees it, with every part a test can change."""

    def __init__(self) -> None:
        self.lines: list[RawLine] = [INVOICES, REPORTS]
        self.error: Exception | None = None
        self.tree: UiaTree = blind_tree()
        self.window_exists = True
        self.minimised = False
        self.cloaked = False
        self.root_at: int | None = HWND
        self.walked: list[int] = []
        self.captured: list[CaptureTarget] = []

    @property
    def engine(self) -> Scripted:
        return Scripted(self.lines, self.error)

    def walk(self, hwnd: int) -> UiaTree:
        self.walked.append(hwnd)
        return self.tree

    def capture(self, target: CaptureTarget) -> Frame:
        self.captured.append(target)
        return white_frame()

    def resolve(self, source: Redacted, line_id: int, **kwargs: object) -> grounding.TextGrounding:
        return resolve_text(source, line_id, ocr=self.engine, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Screen:
    fake = Screen()
    monkeypatch.setattr(grounding, "walk", fake.walk)
    monkeypatch.setattr(grounding, "capture", fake.capture)
    monkeypatch.setattr(grounding, "verify_layout", lambda layout: layout)
    monkeypatch.setattr(redact_module, "_stacking", lambda: [window()])
    monkeypatch.setattr(display_win32, "is_window", lambda _h: fake.window_exists)
    monkeypatch.setattr(display_win32, "is_minimised", lambda _h: fake.minimised)
    monkeypatch.setattr(display_win32, "is_cloaked", lambda _h: fake.cloaked)
    monkeypatch.setattr(display_win32, "root_window_at", lambda _x, _y: fake.root_at)
    return fake


# --------------------------------------------------------------------------- #
# What is offered
# --------------------------------------------------------------------------- #


def test_every_readable_line_is_offered_by_its_index_with_its_window() -> None:
    targets = text_targets(observed())
    assert [(t.id, t.text, t.box, t.hwnd) for t in targets] == [
        (0, "Invoices", INVOICES_BOX, HWND),
        (1, "Reports", REPORTS_BOX, HWND),
    ]


def test_the_text_of_a_target_is_kept_out_of_its_repr() -> None:
    assert "Invoices" not in repr(text_targets(observed())[0])


def test_a_line_redaction_blanked_is_never_offered_and_the_ids_keep_their_places() -> None:
    source = observed([(f"key {OPENAI_KEY}", (10, 10, 280, 30)), REPORTS])
    assert source.text[0].redacted
    assert [t.id for t in text_targets(source)] == [1]


@pytest.mark.parametrize(
    "line",
    [
        ("   ", (10.0, 10.0, 120.0, 30.0)),
        (".", (10.0, 10.0, 10.0 + MIN_TEXT_PX - 1, 30.0)),
        ("-", (10.0, 10.0, 120.0, 10.0 + MIN_TEXT_PX - 1)),
    ],
)
def test_blank_and_speck_sized_lines_are_not_offered(line: RawLine) -> None:
    source = replace(
        observed([REPORTS]),
        text=(OcrLine(text=line[0], bbox=_desktop(line)), *observed([REPORTS]).text),
    )
    assert [t.id for t in text_targets(source)] == [1]


def test_a_line_two_walked_windows_could_own_is_not_offered() -> None:
    first = observed()
    second_tree = replace(blind_tree(), hwnd=HWND + 1)
    both = replace(first, trees=(*first.trees, redact_module.redact_tree(second_tree)[0]))
    assert text_targets(both) == ()


def test_a_line_outside_every_blind_region_is_not_offered() -> None:
    source = replace(observed(), text=(OcrLine(text="Title", bbox=Rect(60, 52, 120, 68)),))
    assert text_targets(source) == ()


# --------------------------------------------------------------------------- #
# Grounding a line
# --------------------------------------------------------------------------- #


def test_a_line_read_again_in_the_same_place_resolves_to_its_centre(screen: Screen) -> None:
    result = screen.resolve(observed(), 1)
    assert result.point == Point(105, 130)
    assert (result.line_id, result.box, result.hwnd, result.layout) == (
        1,
        REPORTS_BOX,
        HWND,
        LAYOUT,
    )
    assert not result.moved


def test_the_window_is_observed_afresh(screen: Screen) -> None:
    screen.resolve(observed(), 0)
    assert screen.walked == [HWND]
    assert screen.captured == [WindowTarget(HWND)]


@pytest.mark.parametrize("dx", [TEXT_PLACE_PX, -TEXT_PLACE_PX])
def test_a_line_within_the_tolerance_is_clicked_where_it_is_now(screen: Screen, dx: int) -> None:
    screen.lines = [INVOICES, shifted(REPORTS, dx=dx)]
    result = screen.resolve(observed(), 1)
    assert result.moved
    assert result.point == Point(105 + dx, 130)


@pytest.mark.parametrize(("dx", "dy"), [(TEXT_PLACE_PX + 1, 0), (0, TEXT_PLACE_PX + 1), (0, 40)])
def test_a_line_that_moved_is_refused(screen: Screen, dx: int, dy: int) -> None:
    screen.lines = [INVOICES, shifted(REPORTS, dx=dx, dy=dy)]
    with pytest.raises(GroundingError, match="changed or moved"):
        screen.resolve(observed(), 1)


@pytest.mark.parametrize("edge", range(4))
def test_any_one_edge_past_the_tolerance_is_refused(screen: Screen, edge: int) -> None:
    """A box that grew or shrank on one side is a different read of the line."""
    text, box = REPORTS
    grown = list(box)
    grown[edge] += (TEXT_PLACE_PX + 1) * (-1 if edge < 2 else 1)
    screen.lines = [INVOICES, (text, (grown[0], grown[1], grown[2], grown[3]))]
    with pytest.raises(GroundingError, match="changed or moved"):
        screen.resolve(observed(), 1)


def test_a_line_whose_text_changed_is_refused(screen: Screen) -> None:
    screen.lines = [INVOICES, ("Recycle bin", REPORTS[1])]
    with pytest.raises(GroundingError, match="changed or moved"):
        screen.resolve(observed(), 1)


def test_a_line_that_now_shows_a_secret_is_refused(screen: Screen) -> None:
    """The re-read goes through redaction, so a secret read there cannot match."""
    screen.lines = [INVOICES, (f"Reports {OPENAI_KEY}", REPORTS[1])]
    with pytest.raises(GroundingError, match="changed or moved"):
        screen.resolve(observed(), 1)


def test_a_line_read_twice_in_the_same_place_is_refused(screen: Screen) -> None:
    screen.lines = [INVOICES, REPORTS, shifted(REPORTS, dx=1)]
    with pytest.raises(GroundingError, match="told apart"):
        screen.resolve(observed(), 1)


def test_a_region_ocr_cannot_read_again_is_refused(screen: Screen) -> None:
    screen.error = OcrError("no engine")
    with pytest.raises(GroundingError, match="could not be read again"):
        screen.resolve(observed(), 1)


def test_another_window_at_the_point_is_refused(screen: Screen) -> None:
    screen.root_at = HWND + 1
    with pytest.raises(GroundingError, match="Another window is covering text line 1"):
        screen.resolve(observed(), 1)


def test_a_changed_display_layout_voids_the_observation(screen: Screen) -> None:
    other = Rect(0, 0, SCREEN.width + 1, SCREEN.height)
    layout = DisplayLayout(monitors=(Monitor("D0", other, other, 96, True),), virtual=other)
    screen.tree = replace(screen.tree, layout=layout)
    with pytest.raises(LayoutChangedError):
        screen.resolve(observed(), 1)
    assert screen.captured == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("window_exists", False, "closed"),
        ("minimised", True, "minimised"),
        ("cloaked", True, "another virtual desktop"),
    ],
)
def test_a_window_that_cannot_be_clicked_into_is_refused_before_it_is_read(
    screen: Screen, field: str, value: bool, message: str
) -> None:
    setattr(screen, field, value)
    with pytest.raises(GroundingError, match=message):
        screen.resolve(observed(), 1)
    assert screen.walked == []


@pytest.mark.parametrize("line_id", [2, -1, 99])
def test_an_id_that_is_not_a_line_is_refused_before_anything_is_read(
    screen: Screen, line_id: int
) -> None:
    with pytest.raises(GroundingError, match="not one of the offered lines"):
        screen.resolve(observed(), line_id)
    assert screen.walked == []


def test_an_id_the_model_was_not_shown_is_refused(screen: Screen) -> None:
    with pytest.raises(GroundingError, match="not one of the offered lines"):
        screen.resolve(observed(), 1, offered={0})
    assert screen.walked == []


def test_a_redacted_line_is_refused_even_by_its_own_id(screen: Screen) -> None:
    source = observed([(f"key {OPENAI_KEY}", (10, 10, 280, 30)), REPORTS])
    with pytest.raises(GroundingError, match="not one of the offered lines"):
        screen.resolve(source, 0)


@pytest.mark.parametrize("line_id", [True, "1", 1.0, None])
def test_an_id_that_is_not_a_whole_number_is_refused(screen: Screen, line_id: object) -> None:
    with pytest.raises(GroundingError, match="whole number"):
        screen.resolve(observed(), line_id)  # type: ignore[arg-type]


def test_every_refusal_says_what_to_do_next(screen: Screen) -> None:
    refusals: list[Callable[[], None]] = [
        lambda: setattr(screen, "lines", []),
        lambda: setattr(screen, "error", OcrError("x")),
        lambda: setattr(screen, "root_at", None),
        lambda: setattr(screen, "lines", [INVOICES, REPORTS, REPORTS]),
    ]
    for setup in refusals:
        fresh = Screen()
        vars(screen).update(vars(fresh))
        setup()
        with pytest.raises(GroundingError) as caught:
            screen.resolve(observed(), 1)
        assert str(caught.value).endswith(OBSERVE_AGAIN)


def test_no_screen_text_reaches_the_log(screen: Screen, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    screen.resolve(observed(), 1)
    with pytest.raises(GroundingError):
        screen.lines = [INVOICES, ("Recycle bin", REPORTS[1])]
        screen.resolve(observed(), 1)
    logged = " ".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    for text in ("Invoices", "Reports", "Recycle bin"):
        assert text not in logged


def _desktop(line: RawLine) -> Rect:
    _, (left, top, right, bottom) = line
    return Rect(
        PAGE.left + int(left), PAGE.top + int(top), PAGE.left + int(right), PAGE.top + int(bottom)
    )


# --------------------------------------------------------------------------- #
# Live: a real window that draws its own text
# --------------------------------------------------------------------------- #

LINES = ("Open the invoice folder", "Export report 2026", "Settings")


@pytest.fixture
def canvas() -> Iterator[CanvasWindow]:
    ensure_dpi_awareness()
    work = query_layout().primary.work_area
    rect = Rect(work.left + 200, work.top + 200, work.left + 1100, work.top + 600)
    with CanvasWindow(rect, LINES) as opened:
        yield opened


def _observe(hwnd: int) -> Redacted:
    tree = walk(hwnd)
    return redact(capture(WindowTarget(hwnd)), [tree], ocr=SYSTEM_OCR)


def _target(source: Redacted, text: str) -> TextTarget:
    return next(t for t in text_targets(source) if t.text == text)


def test_live_every_drawn_line_is_offered_and_grounded_on_its_own_pixels(
    canvas: CanvasWindow,
) -> None:
    source = _observe(canvas.hwnd)
    assert sorted(t.text for t in text_targets(source)) == sorted(LINES)
    for index, text in enumerate(LINES):
        target = _target(source, text)
        result = resolve_text(source, target.id, offered={target.id})
        band = canvas.line_rect(index)
        assert band.top <= result.point.y < band.bottom, text
        assert canvas.rect.left <= result.point.x < canvas.rect.right, text
        assert result.hwnd == canvas.hwnd
        assert not result.moved


def test_live_a_line_the_app_redrew_with_other_text_is_refused(canvas: CanvasWindow) -> None:
    source = _observe(canvas.hwnd)
    target = _target(source, "Settings")
    canvas.set_lines((LINES[0], LINES[1], "Delete everything"))
    with pytest.raises(GroundingError, match="changed or moved"):
        resolve_text(source, target.id)


def test_live_a_line_that_scrolled_is_refused(canvas: CanvasWindow) -> None:
    source = _observe(canvas.hwnd)
    target = _target(source, "Export report 2026")
    canvas.set_lines(LINES[1:])
    with pytest.raises(GroundingError, match="changed or moved"):
        resolve_text(source, target.id)
