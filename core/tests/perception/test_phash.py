"""Tests for `perception/phash.py` — the hash the stuck detector compares.

Three things matter: the same screen always hashes the same, a change the agent
could have caused moves the hash past `NEAR_IDENTICAL_BITS` while noise does not,
and what is hashed is the frame **after** redaction.
"""

from __future__ import annotations

import logging
import random
import sys
import time
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.phash import (  # noqa: E402
    DEAD_BAND,
    GRID_HEIGHT,
    GRID_WIDTH,
    HASH_BITS,
    NEAR_IDENTICAL_BITS,
    PerceptualHash,
    difference_bits,
    phash,
)
from aegis_core.perception.redact import Redacted, redact, redact_tree  # noqa: E402
from aegis_core.perception.screen import Frame, WindowTarget, capture  # noqa: E402
from aegis_core.perception.uia_tree import walk  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from tests.perception.helpers import FormWindow  # noqa: E402
from tests.perception.test_redact import Tree, window  # noqa: E402

SCREEN = Rect(0, 0, 1600, 1000)
LAYOUT = DisplayLayout(
    monitors=(Monitor(device="D0", bounds=SCREEN, work_area=SCREEN, dpi=96, primary=True),),
    virtual=SCREEN,
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def frame_of(image: Image.Image, region: Rect = SCREEN, layout: DisplayLayout = LAYOUT) -> Frame:
    """`image` as a captured frame: top-down BGRX, as GDI writes it."""
    pixels = bytearray(image.convert("RGB").tobytes("raw", "BGRX"))
    return Frame(region=region, layout=layout, captured_at=0.0, pixels=pixels)


def desktop() -> Image.Image:
    """Something shaped like a UI: a title bar, a sidebar, panels and lines of text."""
    image = Image.new("RGB", (SCREEN.width, SCREEN.height), (243, 243, 243))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1599, 48), fill=(32, 32, 32))
    draw.rectangle((0, 48, 280, 999), fill=(225, 228, 232))
    for row in range(20):
        draw.rectangle(
            (24, 80 + 40 * row, 24 + 60 + 9 * (row % 7) * 10, 96 + 40 * row), fill=(90, 90, 90)
        )
    for row in range(24):
        width = 300 + 37 * (row * 11 % 23)
        draw.rectangle((320, 80 + 36 * row, 320 + width, 94 + 36 * row), fill=(40, 40, 40))
    draw.rectangle((1180, 120, 1560, 420), fill=(0, 103, 192))
    return image


def observed(image: Image.Image) -> Redacted:
    """`image` as a redacted observation with nothing painted over it."""
    return Redacted(frame=frame_of(image), trees=(), boxes=())


def with_noise(image: Image.Image, amplitude: int, seed: int = 7) -> Image.Image:
    """`image` with every channel of every pixel nudged by up to `amplitude` levels."""
    rnd = random.Random(seed)  # noqa: S311 - pixel noise for a test, not secrets
    table = bytes(rnd.randint(0, 2 * amplitude) for _ in range(4093))
    data = bytearray(image.tobytes())
    for i, value in enumerate(data):
        data[i] = max(0, min(255, value + table[i % 4093] - amplitude))
    return Image.frombytes("RGB", image.size, bytes(data))


def painted(
    image: Image.Image, box: tuple[int, int, int, int], rgb: tuple[int, int, int]
) -> Image.Image:
    copy = image.copy()
    ImageDraw.Draw(copy).rectangle(box, fill=rgb)
    return copy


def grey_grid(values: list[list[int]]) -> Image.Image:
    grid = Image.new("L", (GRID_WIDTH, GRID_HEIGHT))
    grid.putdata([v for row in values for v in row])
    return grid


# --------------------------------------------------------------------------- #
# The hash value
# --------------------------------------------------------------------------- #


def test_the_hash_has_the_documented_width() -> None:
    assert HASH_BITS == 256
    assert len(PerceptualHash(0).hex) == 64
    assert len(PerceptualHash((1 << HASH_BITS) - 1).hex) == 64


def test_hex_round_trips() -> None:
    for bits in (0, 1, 1 << 255, (1 << 256) - 1, 0x1234_5678_9ABC_DEF0 << 100):
        value = PerceptualHash(bits)
        assert PerceptualHash.from_hex(value.hex) == value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" + "0" * 63,
        " " + "0" * 63,
        "0x" + "0" * 62,
        "0" * 63 + "\n",
    ],
)
def test_from_hex_refuses_anything_hex_did_not_write(text: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex digits"):
        PerceptualHash.from_hex(text)


@pytest.mark.parametrize("bits", [-1, 1 << 256])
def test_a_hash_outside_256_bits_is_refused(bits: int) -> None:
    with pytest.raises(ValueError, match="256 bits"):
        PerceptualHash(bits)


def test_distance_counts_the_differing_bits() -> None:
    a = PerceptualHash(0b1011 << 200)
    b = PerceptualHash(0b0110 << 200)
    assert a.distance(b) == 3
    assert b.distance(a) == 3
    assert a.distance(a) == 0
    assert PerceptualHash(0).distance(PerceptualHash((1 << 256) - 1)) == 256


def test_near_identical_is_inclusive_of_the_threshold() -> None:
    base = PerceptualHash(0)
    assert base.near_identical(PerceptualHash((1 << NEAR_IDENTICAL_BITS) - 1))
    assert not base.near_identical(PerceptualHash((1 << (NEAR_IDENTICAL_BITS + 1)) - 1))


# --------------------------------------------------------------------------- #
# The difference bits
# --------------------------------------------------------------------------- #


def test_a_bit_is_set_only_where_the_left_cell_is_clearly_brighter() -> None:
    flat = [[128] * GRID_WIDTH for _ in range(GRID_HEIGHT)]
    assert difference_bits(grey_grid(flat)) == 0
    first_row_step = [row[:] for row in flat]
    first_row_step[0][0] = 200  # brighter than its right neighbour: the very first bit
    assert difference_bits(grey_grid(first_row_step)) == 1 << (HASH_BITS - 1)
    last_row_step = [row[:] for row in flat]
    last_row_step[-1][-2] = 128 + DEAD_BAND + 1  # the last pair of the last row: the very last bit
    assert difference_bits(grey_grid(last_row_step)) == 1
    darker = [row[:] for row in flat]
    darker[5][5] = 100  # darker than its right neighbour sets nothing there…
    assert difference_bits(grey_grid(darker)) == 1 << (
        HASH_BITS - 1 - (5 * 16 + 4)
    )  # …but its left one


def test_a_difference_inside_the_dead_band_sets_nothing() -> None:
    rows = [[128 + (x % 2) * DEAD_BAND for x in range(GRID_WIDTH)] for _ in range(GRID_HEIGHT)]
    assert difference_bits(grey_grid(rows)) == 0


def test_a_descending_ramp_sets_every_bit() -> None:
    ramp = [[255 - 10 * x for x in range(GRID_WIDTH)] for _ in range(GRID_HEIGHT)]
    assert difference_bits(grey_grid(ramp)) == (1 << HASH_BITS) - 1


@pytest.mark.parametrize(
    "grid",
    [
        Image.new("L", (GRID_WIDTH + 1, GRID_HEIGHT)),
        Image.new("L", (GRID_WIDTH, GRID_HEIGHT - 1)),
        Image.new("RGB", (GRID_WIDTH, GRID_HEIGHT)),
    ],
)
def test_difference_bits_refuses_anything_but_the_grey_grid(grid: Image.Image) -> None:
    with pytest.raises(ValueError, match="grey grid"):
        difference_bits(grid)


# --------------------------------------------------------------------------- #
# Hashing observations
# --------------------------------------------------------------------------- #


def test_the_same_screen_hashes_the_same_every_time() -> None:
    first = phash(observed(desktop()))
    assert phash(observed(desktop())) == first
    assert first.bits != 0, "a UI-shaped frame should set some bits"


def test_a_flat_screen_hashes_to_zero() -> None:
    assert phash(observed(Image.new("RGB", (SCREEN.width, SCREEN.height), (30, 90, 200)))).bits == 0


@pytest.mark.parametrize("amplitude", [1, 2, 4, 8])
def test_noise_stays_near_identical(amplitude: int) -> None:
    base = phash(observed(desktop()))
    noisy = phash(observed(with_noise(desktop(), amplitude)))
    assert base.near_identical(noisy), base.distance(noisy)


@pytest.mark.parametrize(
    ("what", "box"),
    [
        ("a dropdown", (700, 300, 950, 650)),
        ("a dialog", (500, 250, 1100, 650)),
    ],
)
def test_a_change_the_agent_could_cause_is_not_near_identical(
    what: str, box: tuple[int, int, int, int]
) -> None:
    base = phash(observed(desktop()))
    changed = phash(observed(painted(desktop(), box, (252, 252, 252))))
    assert not base.near_identical(changed), f"{what}: {base.distance(changed)} bits"


def test_bigger_changes_move_more_bits() -> None:
    base = phash(observed(desktop()))
    small = base.distance(
        phash(observed(painted(desktop(), (700, 300, 950, 650), (252, 252, 252))))
    )
    large = base.distance(
        phash(observed(painted(desktop(), (300, 100, 1500, 900), (252, 252, 252))))
    )
    assert small < large


@pytest.mark.parametrize(
    "size",
    [(1, 1), (1, 900), (900, 1), (17, 16), (3, 2000), (3840, 2160), (1280, 800)],
)
def test_any_frame_shape_hashes(size: tuple[int, int]) -> None:
    region = Rect(0, 0, *size)
    layout = DisplayLayout(
        monitors=(Monitor(device="D0", bounds=region, work_area=region, dpi=96, primary=True),),
        virtual=region,
    )
    image = Image.linear_gradient("L").resize(size).convert("RGB")
    frame = frame_of(image, region, layout)
    value = phash(Redacted(frame=frame, trees=(), boxes=()))
    assert 0 <= value.bits < 1 << HASH_BITS


def test_what_is_hashed_is_the_frame_after_redaction() -> None:
    """Two screens that differ only inside a password box are the same screen once
    redaction has blacked the box — and they would not be without it."""
    box = Rect(400, 300, 1200, 700)
    t = Tree(root=SCREEN)
    t.add(t.root, "Edit", "Password", bbox=box, is_password=True)
    # Enough labelled text round the box that no part of the window is blind to OCR.
    for row in range(10):
        for column in range(8):
            left, top = 20 + 200 * column, 20 + 100 * row
            t.add(t.root, "Text", "label", bbox=Rect(left, top, left + 160, top + 60))
    tree = t.build(layout=LAYOUT)
    blank = frame_of(desktop())
    striped = desktop()
    draw = ImageDraw.Draw(striped)
    for x in range(box.left, box.right, 40):
        draw.rectangle((x, box.top, x + 19, box.bottom - 1), fill=(255, 255, 255))
    typed = frame_of(striped)

    raw_distance = phash(Redacted(frame=blank, trees=(), boxes=())).distance(
        phash(Redacted(frame=typed, trees=(), boxes=()))
    )
    assert raw_distance > 0, "the fixture must differ where it matters, or this proves nothing"

    stack = [window(bounds=SCREEN)]
    first = redact(blank, [tree], stacking=stack, ocr=None)
    second = redact(typed, [tree], stacking=stack, ocr=None)
    assert first.unread == () == second.unread
    assert phash(first) == phash(second)


def test_hashing_never_touches_the_frame() -> None:
    observation = observed(desktop())
    before = bytes(observation.frame.pixels)
    phash(observation)
    assert bytes(observation.frame.pixels) == before


def test_nothing_but_a_timing_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="aegis_core.perception.phash")
    phash(observed(desktop()))
    [record] = caplog.records
    assert record.getMessage() == "screen.hashed"
    assert set(vars(record)) & {"bits", "hash", "hex"} == set()
    assert isinstance(vars(record)["ms"], float)


def test_hashing_a_full_monitor_frame_is_cheap() -> None:
    region = Rect(0, 0, 3200, 2000)
    layout = DisplayLayout(
        monitors=(Monitor(device="D0", bounds=region, work_area=region, dpi=192, primary=True),),
        virtual=region,
    )
    observation = Redacted(
        frame=frame_of(desktop().resize((region.width, region.height)), region, layout),
        trees=(),
        boxes=(),
    )
    phash(observation)  # warm up
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        phash(observation)
        timings.append(time.perf_counter() - started)
    median = sorted(timings)[2]
    assert median < 0.05, f"{median * 1000:.1f} ms"  # the capture itself takes ~50 ms


# --------------------------------------------------------------------------- #
# Live: a real window, observed twice
# --------------------------------------------------------------------------- #


@pytest.fixture
def form() -> Iterator[FormWindow]:
    ensure_dpi_awareness()
    work = query_layout().primary.work_area
    rect = Rect(work.left + 160, work.top + 160, work.left + 660, work.top + 460)
    with FormWindow(rect) as opened:
        yield opened


def observe(hwnd: int) -> Redacted:
    walked = walk(hwnd)
    frame = capture(WindowTarget(hwnd))
    return redact(frame, [redact_tree(walked)[0]])


def test_a_real_window_observed_twice_is_near_identical(form: FormWindow) -> None:
    first = phash(observe(form.hwnd))
    second = phash(observe(form.hwnd))
    assert first.near_identical(second), first.distance(second)
    assert first.bits != 0, "a real window with controls should set some bits"
