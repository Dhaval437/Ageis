"""Tests for `perception/redact.py` — the gate every screenshot passes on its way out.

`REVIEW.md § 5` and `§ 6` ask for three things, and each has its section here:

* a **golden-image** test — a real password box, captured off the real screen,
  must be pixel-black after redaction, in the frame bytes and in the WebP;
* a test that **fails if redaction is bypassed** — a scan of the core's source
  for any other route from a `Frame` to bytes that could leave the process;
* the rules themselves, on hand-built frames, trees and window stacks.

No credential-shaped fixture is written out literally: each is assembled at run
time, so the repository never holds a string a secret scanner would flag.
"""

from __future__ import annotations

import ast
import io
import logging
import random
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

from aegis_core.perception import redact as redact_module  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.redact import (  # noqa: E402
    PAD_PX,
    REDACTED,
    OnScreenWindow,
    RedactionError,
    _subtract,
    is_secret_label,
    redact,
    redact_text,
    redact_tree,
)
from aegis_core.perception.screen import (  # noqa: E402
    BYTES_PER_PIXEL,
    Frame,
    WindowTarget,
    capture,
)
from aegis_core.perception.uia_tree import UiaElement, UiaTree, walk  # noqa: E402
from PIL import Image  # noqa: E402

from tests.perception.helpers import FormWindow, SolidWindow  # noqa: E402

WHITE = (255, 255, 255)
SCREEN = Rect(0, 0, 400, 300)
LAYOUT = DisplayLayout(
    monitors=(Monitor(device="D0", bounds=SCREEN, work_area=SCREEN, dpi=96, primary=True),),
    virtual=SCREEN,
)
WINDOW = Rect(50, 50, 350, 250)
HWND = 7

# Assembled, never literal — see the module docstring.
OPENAI_KEY = "sk-" + "proj-" + "Q7" * 16
AWS_KEY = "AK" + "IA" + "Z2" * 8
GITHUB_TOKEN = "gh" + "p_" + "a1B2" * 9
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
JWT = ".".join(
    ("ey" + "J" + "hbGciOiJIUzI1NiJ9", "ey" + "J" + "zdWIiOiIxMjM0In0", "c2lnbmF0dXJlXzEyMw")
)
CARD = " ".join(("4111", "1111", "1111", "1111"))  # the standard Visa test number


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def white_frame(region: Rect = SCREEN, layout: DisplayLayout = LAYOUT) -> Frame:
    pixels = bytearray(bytes((255, 255, 255, 0)) * (region.width * region.height))
    return Frame(region=region, layout=layout, captured_at=0.0, pixels=pixels)


def pixel(frame: Frame, x: int, y: int) -> tuple[int, int, int]:
    offset = ((y - frame.region.top) * frame.width + (x - frame.region.left)) * BYTES_PER_PIXEL
    blue, green, red = frame.pixels[offset : offset + 3]
    return red, green, blue


def black_pixels(frame: Frame) -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(frame.region.top, frame.region.bottom)
        for x in range(frame.region.left, frame.region.right)
        if pixel(frame, x, y) == (0, 0, 0)
    }


def darkest(image: Image.Image, at: tuple[int, int]) -> int:
    """The brightest channel of an RGB pixel: 0 is black."""
    value = image.getpixel(at)
    assert isinstance(value, tuple)
    return max(value)


def rect_pixels(*rects: Rect) -> set[tuple[int, int]]:
    return {(x, y) for r in rects for y in range(r.top, r.bottom) for x in range(r.left, r.right)}


class Tree:
    """A `UiaTree` for window `HWND`, built element by element in document order."""

    def __init__(self, root: Rect = WINDOW, hwnd: int = HWND) -> None:
        self.hwnd = hwnd
        self.elements: list[UiaElement] = []
        self.root = self.add(None, "Window", "App", bbox=root)

    def add(
        self,
        parent: int | None,
        role: str,
        name: str = "",
        *,
        value: str | None = None,
        bbox: Rect | None = None,
        is_password: bool = False,
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
                bbox=bbox if bbox is not None else Rect(100, 100 + index, 200, 101 + index),
                enabled=True,
                focused=False,
                offscreen=False,
                is_password=is_password,
                automation_id="",
                class_name="",
                runtime_id=(42, index),
            )
        )
        return index

    def build(self, *, truncated: bool = False, layout: DisplayLayout = LAYOUT) -> UiaTree:
        return UiaTree(
            hwnd=self.hwnd,
            elements=tuple(self.elements),
            truncated=truncated,
            layout=layout,
            captured_at=0.0,
        )


def window(hwnd: int = HWND, bounds: Rect = WINDOW, rect: Rect | None = None) -> OnScreenWindow:
    return OnScreenWindow(hwnd=hwnd, rect=rect or bounds, bounds=bounds)


def padded(rect: Rect) -> Rect:
    return Rect(rect.left - PAD_PX, rect.top - PAD_PX, rect.right + PAD_PX, rect.bottom + PAD_PX)


def outside(inner: Rect, outer: Rect = SCREEN) -> set[tuple[int, int]]:
    return rect_pixels(outer) - rect_pixels(inner)


# --------------------------------------------------------------------------- #
# The tree: fields
# --------------------------------------------------------------------------- #


def test_a_password_field_loses_its_value_keeps_its_label_and_is_flagged() -> None:
    t = Tree()
    field = t.add(t.root, "Edit", "Password", value=None, is_password=True)
    clean, boxes = redact_tree(t.build())
    element = clean.elements[field]
    assert (element.name, element.value, element.redacted) == ("Password", None, True)
    assert boxes == [t.elements[field].bbox]


def test_everything_inside_a_secret_field_is_emptied() -> None:
    t = Tree()
    field = t.add(t.root, "Edit", "Password", is_password=True)
    inner = t.add(field, "Text", "hunter2", value="hunter2")
    deeper = t.add(inner, "Text", "hunter2")
    clean, boxes = redact_tree(t.build())
    for index in (inner, deeper):
        assert (clean.elements[index].name, clean.elements[index].value) == ("", None)
        assert clean.elements[index].redacted
    assert len(boxes) == 3


@pytest.mark.parametrize(
    "label",
    [
        "Password", "Confirm passwords", "Passcode", "Passphrase", "PIN", "Enter PIN code",
        "OTP", "One-time code", "Verification code", "Security code", "2FA code", "CVV",
        "CVC2", "Seed phrase", "Recovery key", "Recovery codes", "Backup codes", "API key",
        "Secret key", "Private key", "Access token", "Client secret", "Mnemonic",
    ],
)  # fmt: skip
def test_a_field_labelled_like_a_secret_is_redacted(label: str) -> None:
    t = Tree()
    field = t.add(t.root, "Edit", label, value="123456")
    clean, boxes = redact_tree(t.build())
    assert clean.elements[field].value is None
    assert clean.elements[field].redacted
    assert boxes == [t.elements[field].bbox]


@pytest.mark.parametrize(
    "label",
    ["Name", "Search", "Passport number", "Shipping address", "Opinion", "Pinterest profile",
     "Spinner", "Compass", "Topic"],
)  # fmt: skip
def test_an_ordinary_field_label_is_left_alone(label: str) -> None:
    assert not is_secret_label(label)
    t = Tree()
    field = t.add(t.root, "Edit", label, value="kept")
    clean, boxes = redact_tree(t.build())
    assert clean.elements[field].value == "kept"
    assert not clean.elements[field].redacted
    assert boxes == []


def test_a_secret_word_on_something_that_is_not_a_field_is_left_alone() -> None:
    t = Tree()
    t.add(t.root, "Button", "Pin to taskbar")
    t.add(t.root, "Hyperlink", "Forgot password?")
    t.add(t.root, "Text", "Enter your PIN below")
    clean, boxes = redact_tree(t.build())
    assert not any(e.redacted for e in clean.elements)
    assert boxes == []


def test_an_unnamed_field_takes_its_label_from_the_text_before_it() -> None:
    # A web form whose <label> is not associated with its <input>.
    t = Tree()
    form = t.add(t.root, "Group", "Sign in")
    t.add(form, "Text", "PIN")
    field = t.add(form, "Edit", value="4321")
    clean, _ = redact_tree(t.build())
    assert clean.elements[field].value is None


def test_a_label_only_reaches_the_field_directly_after_it() -> None:
    t = Tree()
    form = t.add(t.root, "Group", "Sign in")
    t.add(form, "Text", "PIN")
    t.add(form, "Edit", "Name", value="kept")
    later = t.add(form, "Edit", value="kept")
    elsewhere = t.add(t.root, "Edit", value="kept")
    clean, _ = redact_tree(t.build())
    assert clean.elements[later].value == "kept"
    assert clean.elements[elsewhere].value == "kept"


def test_a_redacted_tree_says_so_and_prune_will_take_it() -> None:
    tree = Tree().build()
    assert not tree.redacted
    clean, _ = redact_tree(tree)
    assert clean.redacted


def test_a_tree_with_nothing_secret_is_returned_element_for_element() -> None:
    t = Tree()
    t.add(t.root, "Button", "Save")
    t.add(t.root, "Edit", "Name", value="Ada")
    tree = t.build()
    clean, boxes = redact_tree(tree)
    assert all(a is b for a, b in zip(clean.elements, tree.elements, strict=True))
    assert boxes == []


# --------------------------------------------------------------------------- #
# The tree: credential-shaped text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret", [OPENAI_KEY, AWS_KEY, GITHUB_TOKEN, PEM_HEADER, JWT, CARD])
def test_credential_shaped_text_is_replaced_wherever_it_appears(secret: str) -> None:
    t = Tree()
    label = t.add(t.root, "Text", f"Your key: {secret} (keep it safe)")
    cell = t.add(t.root, "Edit", "Value", value=secret)
    clean, boxes = redact_tree(t.build())
    assert clean.elements[label].name == f"Your key: {REDACTED} (keep it safe)"
    assert clean.elements[cell].value == REDACTED
    assert clean.elements[label].redacted
    assert secret not in repr(clean.elements)
    assert len(boxes) == 2


def test_a_window_title_holding_a_key_is_redacted_too() -> None:
    t = Tree()
    t.elements[0] = replace(t.elements[0], name=f".env — {OPENAI_KEY}")
    clean, _ = redact_tree(t.build())
    assert OPENAI_KEY not in clean.elements[0].name


def test_a_masked_key_is_not_a_credential() -> None:
    assert redact_text("sk-…abcd") == "sk-…abcd"


def test_a_number_that_fails_luhn_is_not_a_card() -> None:
    for text in ("4111 1111 1111 1112", "Order 12345678901234", "+44 20 7946 0958 1234"):
        assert redact_text(text) == text


def test_card_numbers_are_found_in_their_usual_spellings() -> None:
    for spelling in (CARD, CARD.replace(" ", ""), CARD.replace(" ", "-")):
        assert redact_text(f"Card {spelling} expires") == f"Card {REDACTED} expires"


def test_redact_text_is_the_scanner_the_clipboard_will_use() -> None:
    text = f"export OPENAI_API_KEY={OPENAI_KEY}\nexport AWS={AWS_KEY}"
    assert redact_text(text) == f"export OPENAI_API_KEY={REDACTED}\nexport AWS={REDACTED}"


# --------------------------------------------------------------------------- #
# Pixels
# --------------------------------------------------------------------------- #


def test_golden_a_password_fields_padded_box_is_exactly_black_and_nothing_else_is() -> None:
    t = Tree()
    box = Rect(120, 140, 280, 170)
    t.add(t.root, "Edit", "Password", bbox=box, is_password=True)
    result = redact(white_frame(), [t.build()], stacking=[window()])
    assert black_pixels(result.frame) == rect_pixels(padded(box)) | outside(WINDOW)


def test_the_original_frame_is_never_touched() -> None:
    t = Tree()
    t.add(t.root, "Edit", "Password", is_password=True)
    frame = white_frame()
    before = bytes(frame.pixels)
    redact(frame, [t.build()], stacking=[window()])
    assert bytes(frame.pixels) == before


def test_a_frame_with_nothing_to_paint_is_not_copied() -> None:
    frame = white_frame(WINDOW)
    result = redact(frame, [Tree().build()], stacking=[window()])
    assert result.frame is frame
    assert result.boxes == ()


def test_a_box_that_crosses_the_frame_edge_is_clipped_to_it() -> None:
    t = Tree()
    t.add(t.root, "Edit", "PIN", value="1", bbox=Rect(40, 60, 90, 80))
    result = redact(white_frame(WINDOW), [t.build()], stacking=[window()])
    assert result.boxes == (Rect(50, 56, 94, 84),)


def test_the_encoded_screenshot_is_black_where_the_secret_was() -> None:
    t = Tree()
    box = Rect(100, 100, 300, 200)
    t.add(t.root, "Edit", "Password", bbox=box, is_password=True)
    shot = redact(white_frame(), [t.build()], stacking=[window()]).encode()
    image = Image.open(io.BytesIO(shot.data)).convert("RGB")
    for x in range(box.left + 4, box.right - 4, 7):
        for y in range(box.top + 4, box.bottom - 4, 7):
            assert darkest(image, (x, y)) <= 8  # WebP is lossy; black stays black


# --------------------------------------------------------------------------- #
# What is vouched for
# --------------------------------------------------------------------------- #


def test_pixels_outside_every_walked_window_are_black() -> None:
    result = redact(white_frame(), [Tree().build()], stacking=[window()])
    assert black_pixels(result.frame) == outside(WINDOW)


def test_a_walked_window_is_vouched_for_by_its_drawn_bounds_not_its_border() -> None:
    drawn = Rect(60, 50, 340, 240)  # GetWindowRect adds an invisible resize border
    result = redact(white_frame(), [Tree().build()], stacking=[window(bounds=drawn, rect=WINDOW)])
    assert black_pixels(result.frame) == outside(drawn)


def test_an_unwalked_window_stacked_above_the_walked_one_is_black() -> None:
    popup = Rect(300, 200, 380, 280)
    stacking = [window(hwnd=99, bounds=popup), window()]
    result = redact(white_frame(WINDOW), [Tree().build()], stacking=stacking)
    assert black_pixels(result.frame) == rect_pixels(Rect(300, 200, 350, 250))


def test_an_unwalked_window_stacked_below_changes_nothing() -> None:
    stacking = [window(), window(hwnd=99, bounds=Rect(0, 0, 400, 300))]
    result = redact(white_frame(WINDOW), [Tree().build()], stacking=stacking)
    assert result.frame.pixels == white_frame(WINDOW).pixels


def test_two_walked_windows_vouch_for_each_other() -> None:
    lower = Tree()
    upper = Tree(root=Rect(0, 0, 100, 100), hwnd=8)
    stacking = [window(hwnd=8, bounds=Rect(0, 0, 100, 100)), window()]
    result = redact(white_frame(), [lower.build(), upper.build()], stacking=stacking)
    assert black_pixels(result.frame) == outside(WINDOW) - rect_pixels(Rect(0, 0, 100, 100))


def test_a_truncated_tree_vouches_for_nothing() -> None:
    result = redact(white_frame(WINDOW), [Tree().build(truncated=True)], stacking=[window()])
    assert black_pixels(result.frame) == rect_pixels(WINDOW)


def test_no_trees_means_a_black_frame() -> None:
    result = redact(white_frame(), [], stacking=[window()])
    assert black_pixels(result.frame) == rect_pixels(SCREEN)


def test_a_window_that_moved_since_its_walk_is_refused() -> None:
    moved = window(rect=Rect(60, 50, 360, 250), bounds=Rect(60, 50, 360, 250))
    with pytest.raises(RedactionError, match="moved"):
        redact(white_frame(), [Tree().build()], stacking=[moved])


def test_a_walked_window_that_is_not_on_screen_is_refused() -> None:
    with pytest.raises(RedactionError, match="top-level"):
        redact(white_frame(), [Tree().build()], stacking=[window(hwnd=99)])


def test_a_tree_from_another_display_layout_is_refused() -> None:
    other_screen = Rect(0, 0, 800, 600)
    other = DisplayLayout(
        monitors=(Monitor("D0", other_screen, other_screen, 96, True),), virtual=other_screen
    )
    with pytest.raises(RedactionError, match="layout"):
        redact(white_frame(), [Tree().build(layout=other)], stacking=[window()])


def test_subtract_agrees_with_counting_pixels() -> None:
    rng = random.Random(5)  # noqa: S311 - shapes for a geometry test, not secrets
    for _ in range(300):
        base = Rect(0, 0, 30, 30)

        def some_rect() -> Rect:
            x1, x2 = sorted(rng.sample(range(-5, 36), 2))
            y1, y2 = sorted(rng.sample(range(-5, 36), 2))
            return Rect(x1, y1, x2, y2)

        cuts = [some_rect() for _ in range(rng.randint(0, 4))]
        pieces = _subtract([base], cuts)
        expected = rect_pixels(base) - rect_pixels(*cuts)
        covered = [rect_pixels(p) for p in pieces]
        assert set().union(*covered) == expected
        assert sum(len(c) for c in covered) == len(expected)  # pieces never overlap


# --------------------------------------------------------------------------- #
# Hygiene and cost
# --------------------------------------------------------------------------- #


def test_no_screen_text_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    t = Tree()
    t.add(t.root, "Edit", "API key", value=OPENAI_KEY)
    with caplog.at_level(logging.DEBUG, logger="aegis_core.perception.redact"):
        redact(white_frame(), [t.build()], stacking=[window()])
    assert caplog.records
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert OPENAI_KEY not in text
        assert "API key" not in text


def test_redacting_a_4k_frame_is_cheap() -> None:
    screen = Rect(0, 0, 3840, 2160)
    layout = DisplayLayout(monitors=(Monitor("D0", screen, screen, 96, True),), virtual=screen)
    half = Rect(0, 0, 1920, 2160)
    t = Tree(root=half)
    for i in range(50):
        t.add(t.root, "Edit", "Password", bbox=Rect(10, 40 * i, 900, 40 * i + 30), is_password=True)
    frame = white_frame(screen, layout)
    tree = t.build(layout=layout)
    started = time.perf_counter()
    redact(frame, [tree], stacking=[window(bounds=half)])
    elapsed = time.perf_counter() - started
    assert elapsed < 0.1, f"{elapsed * 1000:.0f} ms"  # the whole observation has 400 ms


# --------------------------------------------------------------------------- #
# Bypass: nothing else in the core may turn a frame into outbound bytes
# --------------------------------------------------------------------------- #

CORE = Path(__file__).resolve().parents[2] / "aegis_core"

#: Where a frame may be converted, encoded or read raw, and a screenshot built.
#: `mark.py` draws its numbered overlays at the output scale, so it takes the
#: downscale and the encode as two steps — but only ever on a `Redacted`.
#: `phash.py` shrinks a `Redacted`'s frame to a 17x16 grey grid and keeps 256
#: bits of it; nothing it produces is an image.
ALLOWED = {
    "_encode": {"screen.py", "redact.py"},
    "_encode_image": {"screen.py", "mark.py"},
    "_to_image": {"screen.py", "mark.py", "phash.py"},
    "pixels": {"screen.py", "redact.py"},
    "Screenshot": {"screen.py"},
    "Redacted": {"redact.py"},
    "redacted=": {"redact.py"},
}

#: Names that are a route to frame bytes when read off an object.
ATTRIBUTES = ("_encode", "_encode_image", "_to_image", "pixels")
#: Names that are a route to frame bytes when called outright.
CALLS = ("Screenshot", "Redacted", "_encode_image")


def bypasses(source: str, filename: str) -> list[str]:
    """Every use in `source` of a route from a frame to bytes that `filename` may not take."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in ATTRIBUTES:
            name = node.attr
        elif isinstance(node, ast.keyword) and node.arg == "redacted":
            name = "redacted="
            if filename not in ALLOWED[name]:
                found.append(f"{filename} sets redacted=")
            continue
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if name not in CALLS:
                continue
        else:
            continue
        if filename not in ALLOWED[name]:
            found.append(f"{filename}:{node.lineno} uses {name}")
    return found


def test_nothing_in_the_core_reaches_frame_bytes_except_through_redaction() -> None:
    files = sorted(CORE.rglob("*.py"))
    assert len(files) > 40, "the scan is not looking at the core"
    found = [
        problem
        for path in files
        for problem in bypasses(path.read_text(encoding="utf-8"), path.name)
    ]
    assert found == []


@pytest.mark.parametrize(
    "snippet",
    [
        "shot = frame._encode()",
        "image = frame._to_image()",
        "shot = _encode_image(image, frame)",
        "shot = screen._encode_image(image, frame)",
        "send(frame.pixels)",
        "Screenshot(data=b'', width=1, height=1, region=r, layout_fingerprint='', captured_at=0)",
        "Redacted(frame=frame, trees=(), boxes=()).encode()",
        "screen.Screenshot(data=b'')",
        "tree = dataclasses.replace(tree, redacted=True)",
    ],
)
def test_the_bypass_scan_catches_each_route(snippet: str) -> None:
    assert bypasses(snippet, "context.py")


def test_redact_itself_is_what_encodes() -> None:
    assert "_encode" in Path(redact_module.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Live: the golden image off the real screen
# --------------------------------------------------------------------------- #


@pytest.fixture
def form() -> Iterator[FormWindow]:
    ensure_dpi_awareness()
    work = query_layout().primary.work_area
    with FormWindow(Rect(work.left + 160, work.top + 160, work.left + 660, work.top + 460)) as w:
        time.sleep(0.2)  # let it paint before it is captured
        yield w


def test_golden_a_real_password_box_is_pixel_black_off_the_real_screen(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    frame = capture(WindowTarget(form.hwnd))
    password = form.child_rect("password")
    text = form.child_rect("text")
    assert any(
        pixel(frame, x, y) != (0, 0, 0)
        for y in range(password.top, password.bottom)
        for x in range(password.left, password.right)
    )

    result = redact(frame, [tree])

    assert rect_pixels(password) <= black_pixels(result.frame)
    assert all(pixel(result.frame, x, y) == pixel(frame, x, y) for x, y in rect_pixels(text))
    field = next(e for e in result.trees[0].elements if e.is_password)
    assert field.redacted and field.value is None
    assert FormWindow.SECRET not in repr(result.trees)

    image = Image.open(io.BytesIO(result.encode().data)).convert("RGB")
    scale = image.width / frame.width
    centre = (
        int(((password.left + password.right) / 2 - frame.region.left) * scale),
        int(((password.top + password.bottom) / 2 - frame.region.top) * scale),
    )
    assert darkest(image, centre) <= 8


def test_a_real_window_stacked_over_the_walked_one_is_black(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    frame_rect = form.rect
    cover = Rect(
        frame_rect.right - 120,
        frame_rect.bottom - 90,
        frame_rect.right + 40,
        frame_rect.bottom + 40,
    )
    overlay = SolidWindow(cover)
    try:
        frame = capture(WindowTarget(form.hwnd))
        assert pixel(frame, cover.left + 5, cover.top + 5) == SolidWindow.RGB
        result = redact(frame, [tree])
        assert pixel(result.frame, cover.left + 5, cover.top + 5) == (0, 0, 0)
        assert pixel(result.frame, frame_rect.left + 5, frame_rect.top + 5) != (0, 0, 0)
    finally:
        overlay.destroy()
