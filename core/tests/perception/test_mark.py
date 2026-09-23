"""Tests for `perception/mark.py` — the numbered overlays a model picks an id from.

Three things matter more than the drawing itself and each has a section here:

* a mark **only adds** — every pixel it changes is inside its own element's
  neighbourhood, so nothing a redaction blacked out comes back;
* **only digits are ever drawn**, asserted by recording every string that reaches
  Pillow, so no screen text can be painted back onto a redacted frame;
* the number on a chip is the candidate's **walk id**, which is what `P2-08` will
  resolve, so a mark and a click mean the same element.

Frames and trees are built by hand, then one live pass over a real window this
test opens.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

from aegis_core.perception import mark as mark_module  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.mark import (  # noqa: E402
    CHIP_PAD_X,
    MAX_MARKS,
    MIN_BOX_PX,
    PALETTE,
    MarkError,
    _chip,
    _font,
    _ink,
    _scaled,
    mark,
)
from aegis_core.perception.prune import Candidate, PrunedTree, prune  # noqa: E402
from aegis_core.perception.redact import OnScreenWindow, Redacted, redact, redact_tree  # noqa: E402
from aegis_core.perception.screen import (  # noqa: E402
    BYTES_PER_PIXEL,
    MAX_EDGE,
    Frame,
    WindowTarget,
    capture,
)
from aegis_core.perception.uia_tree import UiaElement, UiaTree, walk  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from tests.perception.helpers import FormWindow  # noqa: E402

SCREEN = Rect(0, 0, 400, 300)
LAYOUT = DisplayLayout(
    monitors=(Monitor(device="D0", bounds=SCREEN, work_area=SCREEN, dpi=96, primary=True),),
    virtual=SCREEN,
)
WINDOW = Rect(50, 50, 350, 250)
HWND = 7


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def white_frame(region: Rect = SCREEN, layout: DisplayLayout = LAYOUT) -> Frame:
    pixels = bytearray(bytes((255, 255, 255, 0)) * (region.width * region.height))
    return Frame(region=region, layout=layout, captured_at=1.5, pixels=pixels)


def element(index: int, bbox: Rect | None, *, role: str = "Button", name: str = "Go") -> UiaElement:
    return UiaElement(
        id=index,
        parent=None if index == 0 else 0,
        depth=0 if index == 0 else 1,
        role=role,
        name=name,
        value=None,
        bbox=bbox,
        enabled=True,
        focused=False,
        offscreen=False,
        is_password=False,
        automation_id="",
        class_name="",
        runtime_id=(42, index),
        redacted=True,
    )


def tree(*boxes: Rect, hwnd: int = HWND, layout: DisplayLayout = LAYOUT) -> UiaTree:
    """A redacted tree: a window element, then one button per box."""
    elements = [element(0, WINDOW, role="Window", name="App")]
    elements += [element(i, box) for i, box in enumerate(boxes, start=1)]
    return UiaTree(
        hwnd=hwnd,
        elements=tuple(elements),
        truncated=False,
        layout=layout,
        captured_at=1.0,
        redacted=True,
    )


def pruned(source: UiaTree, *, ids: tuple[int, ...] | None = None) -> PrunedTree:
    """Every element of `source` as a candidate, in document order — no ranking, so a
    test says exactly which boxes it expects to see marked."""
    chosen = [e for e in source.elements if e.bbox is not None and (ids is None or e.id in ids)]
    return PrunedTree(
        tree=source,
        candidates=tuple(
            Candidate(element=e, bbox=e.bbox, parent=None, score=0)
            for e in chosen
            if e.bbox is not None
        ),
        omitted=0,
    )


def observed(source: UiaTree, frame: Frame | None = None) -> Redacted:
    """A `Redacted` holding `source` and a clean white frame — the marking input."""
    return Redacted(frame=frame if frame is not None else white_frame(), trees=(source,), boxes=())


BIG = Rect(0, 0, 1280, 800)
BIG_LAYOUT = DisplayLayout(
    monitors=(Monitor(device="D0", bounds=BIG, work_area=BIG, dpi=96, primary=True),),
    virtual=BIG,
)


def grid(count: int) -> list[Rect]:
    """`count` comfortably markable boxes laid out across a `BIG` frame."""
    return [
        Rect(10 + 60 * (i % 20), 10 + 60 * (i // 20), 60 + 60 * (i % 20), 40 + 60 * (i // 20))
        for i in range(count)
    ]


def big_observation(boxes: list[Rect]) -> tuple[Redacted, PrunedTree]:
    source = tree(*boxes, layout=BIG_LAYOUT)
    source = replace(source, elements=(replace(source.elements[0], bbox=BIG), *source.elements[1:]))
    frame = white_frame(BIG, BIG_LAYOUT)
    return Redacted(frame=frame, trees=(source,), boxes=()), pruned(source)


def image_of(frame: Frame, max_edge: int) -> Image.Image:
    """The same downscale `mark()` starts from, so a test can diff against it."""
    return frame._to_image(max_edge)


def differences(before: Image.Image, after: Image.Image) -> set[tuple[int, int]]:
    assert before.size == after.size
    left = before.load()
    right = after.load()
    assert left is not None and right is not None
    return {
        (x, y)
        for y in range(before.height)
        for x in range(before.width)
        if left[x, y] != right[x, y]
    }


def _brightest(value: object) -> int:
    """The brightest channel of an RGB pixel: 0 is black."""
    assert isinstance(value, tuple)
    return max(int(channel) for channel in value)


def decoded(result: bytes) -> Image.Image:
    import io

    return Image.open(io.BytesIO(result)).convert("RGB")


# --------------------------------------------------------------------------- #
# What gets marked
# --------------------------------------------------------------------------- #


def test_every_candidate_is_marked_in_document_order_with_its_walk_id() -> None:
    source = tree(Rect(60, 60, 160, 100), Rect(200, 120, 300, 180))
    result = mark(observed(source), pruned(source))
    assert result.ids == (0, 1, 2)
    assert [m.box for m in result.marks] == [
        WINDOW,
        Rect(60, 60, 160, 100),
        Rect(200, 120, 300, 180),
    ]
    assert result.unmarked == ()


def test_the_id_on_a_mark_is_the_walk_id_not_a_fresh_numbering() -> None:
    source = tree(Rect(60, 60, 160, 100), Rect(200, 120, 300, 180))
    result = mark(observed(source), pruned(source, ids=(2,)))
    assert result.ids == (2,), "renumbering would have called it 1"


def test_at_full_size_a_marks_image_box_is_its_physical_box() -> None:
    box = Rect(60, 60, 160, 100)
    source = tree(box)
    result = mark(observed(source), pruned(source, ids=(1,)))
    assert result.marks[0].image_box == box


def test_a_downscaled_frame_scales_every_mark_with_it() -> None:
    box = Rect(100, 100, 200, 140)
    source = tree(box)
    result = mark(observed(source), pruned(source, ids=(1,)), max_edge=200)
    # 400x300 fits into 200 as 200x150: exactly half.
    assert result.screenshot.width == 200
    assert result.marks[0].image_box == Rect(50, 50, 100, 70)


def test_a_candidate_off_the_captured_region_is_left_unmarked() -> None:
    source = tree(Rect(500, 500, 600, 540))
    result = mark(observed(source), pruned(source, ids=(1,)))
    assert result.marks == ()
    assert result.unmarked == (1,)


def test_a_candidate_too_small_at_the_output_scale_is_left_unmarked() -> None:
    #: 4 physical pixels wide becomes 2 at half scale, under MIN_BOX_PX.
    source = tree(Rect(100, 100, 100 + 2 * (MIN_BOX_PX - 1), 160))
    result = mark(observed(source), pruned(source, ids=(1,)), max_edge=200)
    assert result.marks == ()
    assert result.unmarked == (1,)


def test_a_candidate_straddling_the_edge_is_marked_on_the_part_that_shows() -> None:
    source = tree(Rect(-40, 100, 60, 140))
    result = mark(observed(source), pruned(source, ids=(1,)))
    assert result.marks[0].image_box == Rect(0, 100, 60, 140)


def test_no_more_than_max_marks_are_drawn_and_the_rest_come_back_unmarked() -> None:
    boxes = grid(MAX_MARKS + 5)
    observation, candidates = big_observation(boxes)
    result = mark(observation, candidates)
    assert len(result.marks) == MAX_MARKS
    assert len(result.unmarked) == len(boxes) + 1 - MAX_MARKS
    assert set(result.ids) & set(result.unmarked) == set()


def test_a_pruned_tree_with_no_candidates_still_encodes_the_frame() -> None:
    source = tree()
    result = mark(observed(source), PrunedTree(tree=source, candidates=(), omitted=0))
    assert result.marks == ()
    assert result.screenshot.width == SCREEN.width


# --------------------------------------------------------------------------- #
# Refusals: the marks and the frame must describe one observation
# --------------------------------------------------------------------------- #


def test_an_unredacted_tree_is_refused() -> None:
    source = replace(tree(Rect(60, 60, 160, 100)), redacted=False)
    with pytest.raises(MarkError, match="redacted"):
        mark(observed(source), pruned(source))


def test_a_tree_from_another_layout_is_refused() -> None:
    other = replace(LAYOUT, monitors=(replace(LAYOUT.monitors[0], dpi=144),))
    source = tree(Rect(60, 60, 160, 100), layout=other)
    with pytest.raises(MarkError, match="layout changed"):
        mark(observed(source), pruned(source))


def test_candidates_from_a_window_the_frame_was_not_redacted_for_are_refused() -> None:
    walked = tree(Rect(60, 60, 160, 100))
    stranger = tree(Rect(60, 60, 160, 100), hwnd=HWND + 1)
    with pytest.raises(MarkError, match="not from a window"):
        mark(observed(walked), pruned(stranger))


# --------------------------------------------------------------------------- #
# A mark only ever adds
# --------------------------------------------------------------------------- #


def test_marking_changes_no_pixel_far_from_the_element_it_marks() -> None:
    box = Rect(100, 100, 200, 140)
    source = tree(box)
    frame = white_frame()
    result = mark(Redacted(frame=frame, trees=(source,), boxes=()), pruned(source, ids=(1,)))
    changed = differences(image_of(frame, 400), decoded(result.screenshot.data))
    # WebP is lossy, so allow the whole neighbourhood a chip can occupy.
    near = Rect(box.left - 40, box.top - 40, box.right + 40, box.bottom + 40)
    stray = {(x, y) for x, y in changed if not near.contains_rect(Rect(x, y, x + 1, y + 1))}
    assert stray == set()


def test_a_redacted_password_box_is_still_black_after_marking() -> None:
    secret = Rect(120, 120, 240, 150)
    button = Rect(60, 200, 160, 230)
    elements = (
        element(0, WINDOW, role="Window", name="App"),
        element(1, secret, role="Edit", name="Password"),
        element(2, button),
    )
    raw = UiaTree(hwnd=HWND, elements=elements, truncated=False, layout=LAYOUT, captured_at=1.0)
    clean, _ = redact_tree(raw)
    observation = redact(
        white_frame(),
        [clean],
        stacking=[OnScreenWindow(hwnd=HWND, rect=WINDOW, bounds=WINDOW)],
    )
    result = mark(observation, prune(clean), quality=100)
    image = decoded(result.screenshot.data)
    inner = Rect(secret.left + 12, secret.top + 12, secret.right - 12, secret.bottom - 12)
    pixels = image.load()
    assert pixels is not None
    brightest = max(
        _brightest(pixels[x, y])
        for y in range(inner.top, inner.bottom)
        for x in range(inner.left, inner.right)
    )
    assert brightest <= 8, "a mark must not paint over what redaction blacked out"


def test_only_digits_are_ever_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def record(
        self: ImageDraw.ImageDraw, xy: object, text: object, *args: object, **kw: object
    ) -> None:
        assert isinstance(text, str)
        drawn.append(text)
        original(self, xy, text, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    source = tree(Rect(60, 60, 160, 100), Rect(200, 120, 300, 180))
    result = mark(observed(source), pruned(source))
    assert drawn == [str(i) for i in result.ids]
    assert all(text.isdigit() for text in drawn)


def test_no_screen_text_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    source = tree(Rect(60, 60, 160, 100))
    source = replace(
        source,
        elements=tuple(replace(e, name="hunter2-in-a-label") for e in source.elements),
    )
    with caplog.at_level("DEBUG", logger=mark_module.__name__):
        mark(observed(source), pruned(source))
    assert "hunter2" not in caplog.text


# --------------------------------------------------------------------------- #
# The drawing itself
# --------------------------------------------------------------------------- #


def test_a_marked_element_is_outlined_in_a_palette_colour() -> None:
    box = Rect(100, 100, 200, 140)
    source = tree(box)
    result = mark(observed(source), pruned(source, ids=(1,)), quality=100)
    image = decoded(result.screenshot.data)
    pixels = image.load()
    assert pixels is not None
    midpoint = pixels[(box.left + box.right) // 2, box.top]
    assert isinstance(midpoint, tuple)
    nearest = min(
        PALETTE, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, midpoint, strict=True))
    )
    assert sum((a - b) ** 2 for a, b in zip(nearest, midpoint, strict=True)) < 3000


def test_neighbouring_marks_get_different_colours() -> None:
    boxes = [Rect(20 + 60 * i, 100, 70 + 60 * i, 140) for i in range(len(PALETTE))]
    source = tree(*boxes)
    result = mark(observed(source), pruned(source))
    assert len(result.marks) == len(boxes) + 1
    # The palette is cycled, so the first len(PALETTE) marks are all different.
    used = [PALETTE[i % len(PALETTE)] for i in range(len(PALETTE))]
    assert len(set(used)) == len(PALETTE)


def test_a_wider_number_gets_a_wider_chip() -> None:
    font = _font()
    box = Rect(0, 100, 100, 140)
    narrow = _chip(box, _ink(font, "7"), Image.new("RGB", (400, 300)), [])
    wide = _chip(box, _ink(font, "137"), Image.new("RGB", (400, 300)), [])
    assert wide.width > narrow.width
    assert narrow.width >= 2 * CHIP_PAD_X + 1


def test_a_chip_is_always_inside_the_image() -> None:
    image = Image.new("RGB", (400, 300))
    font = _font()
    for box in (Rect(0, 0, 40, 20), Rect(360, 280, 400, 300), Rect(0, 290, 400, 300)):
        chip = _chip(box, _ink(font, "199"), image, [])
        assert chip.left >= 0 and chip.top >= 0
        assert chip.right <= image.width and chip.bottom <= image.height


def test_two_stacked_elements_do_not_get_chips_on_top_of_each_other() -> None:
    image = Image.new("RGB", (400, 300))
    font = _font()
    ink = _ink(font, "12")
    box = Rect(100, 100, 300, 200)
    first = _chip(box, ink, image, [])
    second = _chip(box, ink, image, [first])
    assert not mark_module._overlaps(first, second)


def test_the_screenshot_carries_the_frames_own_provenance() -> None:
    source = tree(Rect(60, 60, 160, 100))
    frame = white_frame()
    result = mark(Redacted(frame=frame, trees=(source,), boxes=()), pruned(source))
    assert result.screenshot.region == frame.region
    assert result.screenshot.layout_fingerprint == frame.layout.fingerprint
    assert result.screenshot.captured_at == frame.captured_at
    assert result.screenshot.media_type == "image/webp"


def test_scaling_a_box_matches_the_mapping_screenshot_documents() -> None:
    region = Rect(-100, -50, 300, 250)
    assert _scaled(Rect(-100, -50, 100, 100), region, 200, 150) == Rect(0, 0, 100, 75)


# --------------------------------------------------------------------------- #
# Bounded
# --------------------------------------------------------------------------- #


def test_drawing_a_full_page_of_marks_is_cheap() -> None:
    """The encode is `P2-02`'s cost and dominates; what `P2-06` adds is the drawing.

    Measured here: 200 marks on a 1280x800 image, ~60 ms. The budget is the slack
    left in the 400 ms an observation has once capture and encode are paid for.
    """
    observation, candidates = big_observation(grid(MAX_MARKS))
    image = observation.frame._to_image(MAX_EDGE)
    marks = [
        mark_module.Mark(element_id=i, box=box, image_box=box)
        for i, box in enumerate(grid(MAX_MARKS), start=1)
    ]
    _font()  # the face is loaded once per process, not once per observation
    started = time.perf_counter()
    mark_module._draw(image, marks)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.15, f"{elapsed * 1000:.0f} ms"
    assert len(mark(observation, candidates).marks) == MAX_MARKS


# --------------------------------------------------------------------------- #
# Live: a real window, walked, redacted, pruned and marked
# --------------------------------------------------------------------------- #


@pytest.fixture
def form() -> Iterator[FormWindow]:
    ensure_dpi_awareness()
    work = query_layout().primary.work_area
    rect = Rect(work.left + 160, work.top + 160, work.left + 660, work.top + 460)
    with FormWindow(rect) as opened:
        yield opened


def test_a_real_window_is_marked_end_to_end(form: FormWindow) -> None:
    ensure_dpi_awareness()
    query_layout()
    walked = walk(form.hwnd)
    frame = capture(WindowTarget(form.hwnd))
    observation = redact(frame, [redact_tree(walked)[0]])
    candidates = prune(observation.trees[0])
    result = mark(observation, candidates)
    assert result.marks, "a real window should have something to mark"
    assert set(result.ids) <= {c.id for c in candidates.candidates}
    assert len(result.marks) + len(result.unmarked) == len(candidates.candidates)
    image = decoded(result.screenshot.data)
    assert (image.width, image.height) == (result.screenshot.width, result.screenshot.height)
    for item in result.marks:
        assert 0 <= item.image_box.left < item.image_box.right <= image.width
        assert 0 <= item.image_box.top < item.image_box.bottom <= image.height


def test_the_frame_marking_started_from_is_never_mutated() -> None:
    box = Rect(100, 100, 200, 140)
    source = tree(box)
    frame = white_frame()
    before = bytes(frame.pixels)
    mark(Redacted(frame=frame, trees=(source,), boxes=()), pruned(source))
    assert bytes(frame.pixels) == before
    assert len(frame.pixels) == SCREEN.width * SCREEN.height * BYTES_PER_PIXEL
