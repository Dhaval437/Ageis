"""Set-of-mark: numbered boxes drawn over the candidate elements of one window.

`mark(redacted, pruned)` takes a redacted observation and the candidates
`prune()` chose from it, draws a coloured outline and a numbered chip over each
one, and returns the encoded `Screenshot` together with the marks it drew. The
model is then asked for an **element id**, never a coordinate
(`ARCHITECTURE.md § 6.3`, `REVIEW.md § 4`).

Four things decide the shape of this module:

* **It draws on a `Redacted`, never on a `Frame`.** Redaction is what makes a
  frame something that may leave the process (invariant 8), and marking happens
  after it, so the only pixels a mark can sit on are pixels already cleared to
  leave. The type says so, and `tests/perception/test_redact.py`'s source scan
  keeps it that way.
* **The number on a chip is the element's walk `id`**, not a fresh 1..N
  numbering. `prune()` deliberately keeps that id so the marks a model reads and
  `P2-08`'s `resolve(element_id)` index the same `UiaTree`; a second numbering
  would be a mapping to keep in step, and a mapping that can drift.
* **Marks are drawn after the downscale**, at the size the model actually sees.
  Drawing at full resolution and then shrinking would put a 2 px outline and a
  13 px number through a box filter and hand the model grey smudges.
* **Nothing but digits is ever drawn.** No name, no value, no label — so a mark
  cannot put back on screen a word redaction just took off it.

One pruned tree at a time: walk ids are unique within a walk and not across
walks, so marking two windows onto one frame would need an id scheme that
`P2-08` does not have. A frame showing more than one walked window is marked for
the window the model is being asked about.

What goes unmarked: a candidate whose visible rectangle falls outside the
captured region, or shrinks below `MIN_BOX_PX` at the output scale — a mark that
small says nothing about which pixel it means. Their ids come back in
`Marked.unmarked` so the caller can drop them from the list it sends rather than
offer the model an id it cannot see.

Every name here is untrusted screen text, and none of it is read, drawn or
logged: the debug line carries counts and timings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from aegis_core.perception.display import Rect
from aegis_core.perception.prune import PrunedTree
from aegis_core.perception.redact import Redacted
from aegis_core.perception.screen import MAX_EDGE, WEBP_QUALITY, Screenshot, _encode_image

log = logging.getLogger(__name__)

#: `REVIEW.md § 2`: bounded. `prune()` caps candidates at 200 by default, but its
#: `limit` goes higher, and a screenshot carrying more marks than that is unreadable
#: to a person and to a model alike.
MAX_MARKS: Final = 200

#: Output pixels. A box narrower or shorter than this is all outline, so a mark on
#: it points at no pixel in particular.
MIN_BOX_PX: Final = 3

#: The outline round a marked element.
OUTLINE_PX: Final = 2

#: Height of the digits on a chip, in output pixels. Measured against a 1280 px
#: capture of a 3200 px panel: smaller than this and the numbers stop being legible
#: once WebP has been over them.
LABEL_FONT_PX: Final = 13

#: Space round the digits inside their chip.
CHIP_PAD_X: Final = 3
CHIP_PAD_Y: Final = 1

#: Corner radius of a chip.
CHIP_RADIUS: Final = 3

#: Drawn round every chip so a light chip on a light background still has an edge.
CHIP_EDGE: Final = (0, 0, 0)

#: Cycled in document order, which is roughly reading order, so neighbouring marks
#: get different colours and a chip is unambiguously the box it belongs to. Every
#: one is saturated enough to be seen on white and on the black of a redaction box.
PALETTE: Final = (
    (255, 59, 48),  # red
    (255, 204, 0),  # amber
    (48, 209, 88),  # green
    (10, 200, 255),  # cyan
    (255, 64, 190),  # magenta
    (150, 120, 255),  # violet
)

_WHITE: Final = (255, 255, 255)
_BLACK: Final = (0, 0, 0)


class MarkError(RuntimeError):
    """The candidates and the frame do not describe the same observation."""


@dataclass(frozen=True, slots=True)
class Mark:
    """One numbered box on the screenshot.

    `element_id` is the candidate's walk `id` — the number drawn on the chip and
    the id the model answers with. `box` is the element in physical virtual-desktop
    pixels; `image_box` is where the outline landed in the screenshot's own pixels.
    """

    element_id: int
    box: Rect
    image_box: Rect


@dataclass(frozen=True, slots=True, eq=False)
class Marked:
    """A marked screenshot and the ids on it."""

    screenshot: Screenshot
    marks: tuple[Mark, ...]
    #: Candidates left unmarked: off the captured region, too small to mark, or
    #: past `MAX_MARKS`. The caller should not offer these ids to the model.
    unmarked: tuple[int, ...] = field(default=())

    @property
    def ids(self) -> tuple[int, ...]:
        """The element ids a model may answer with, in document order."""
        return tuple(mark.element_id for mark in self.marks)


def mark(
    redacted: Redacted,
    pruned: PrunedTree,
    *,
    max_edge: int = MAX_EDGE,
    quality: int = WEBP_QUALITY,
) -> Marked:
    """`redacted` encoded as a screenshot with `pruned`'s candidates numbered on it.

    `pruned` must be the pruning of one of `redacted`'s own trees: anything else
    would put every box somewhere the frame does not show, and raises `MarkError`.
    """
    frame = redacted.frame
    tree = pruned.tree
    if not tree.redacted:
        raise MarkError("A UI tree must be redacted before its elements are marked.")
    if tree.layout != frame.layout:
        raise MarkError("The screen layout changed between reading a window and capturing it.")
    if all(tree.hwnd != other.hwnd for other in redacted.trees):
        raise MarkError("Those candidates are not from a window this frame was redacted for.")

    started = time.perf_counter()
    image = frame._to_image(max_edge)
    region = frame.region
    placed: list[Mark] = []
    unmarked: list[int] = []
    for candidate in pruned.candidates:
        box = _clip(_scaled(candidate.bbox, region, image.width, image.height), image)
        if box is None or len(placed) >= MAX_MARKS:
            unmarked.append(candidate.id)
            continue
        placed.append(Mark(element_id=candidate.id, box=candidate.bbox, image_box=box))
    _draw(image, placed)
    result = Marked(
        screenshot=_encode_image(image, frame, quality=quality),
        marks=tuple(placed),
        unmarked=tuple(unmarked),
    )
    log.debug(
        "screen.marked",
        extra={
            "candidates": len(pruned.candidates),
            "marks": len(placed),
            "unmarked": len(unmarked),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return result


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _scaled(box: Rect, region: Rect, width: int, height: int) -> Rect:
    """`box` in physical pixels, expressed in the pixels of a `width` x `height` image
    of `region` — the inverse of the mapping `Screenshot` documents."""

    def x(value: int) -> int:
        return round((value - region.left) * width / region.width)

    def y(value: int) -> int:
        return round((value - region.top) * height / region.height)

    return Rect(x(box.left), y(box.top), x(box.right), y(box.bottom))


def _clip(box: Rect, image: Image.Image) -> Rect | None:
    """`box` cut down to the image, or `None` if what is left is too small to mark."""
    clipped = Rect(
        max(box.left, 0),
        max(box.top, 0),
        min(box.right, image.width),
        min(box.bottom, image.height),
    )
    if clipped.width < MIN_BOX_PX or clipped.height < MIN_BOX_PX:
        return None
    return clipped


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


def _draw(image: Image.Image, marks: list[Mark]) -> None:
    """Outline and number every mark, in place."""
    if not marks:
        return
    draw = ImageDraw.Draw(image)
    font = _font()
    chips: list[Rect] = []
    for index, item in enumerate(marks):
        colour = PALETTE[index % len(PALETTE)]
        box = item.image_box
        draw.rectangle(
            (box.left, box.top, box.right - 1, box.bottom - 1), outline=colour, width=OUTLINE_PX
        )
        label = str(item.element_id)
        ink = _ink(font, label)
        chip = _chip(box, ink, image, chips)
        chips.append(chip)
        draw.rounded_rectangle(
            (chip.left, chip.top, chip.right - 1, chip.bottom - 1),
            radius=CHIP_RADIUS,
            fill=colour,
            outline=CHIP_EDGE,
        )
        draw.text(
            (chip.left + CHIP_PAD_X - ink[0], chip.top + CHIP_PAD_Y - ink[1]),
            label,
            font=font,
            fill=_contrast(colour),
        )


def _ink(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont, label: str
) -> tuple[int, int, int, int]:
    """The box the digits of `label` actually cover, as whole pixels.

    A scalable face measures in fractions; a chip is drawn in pixels.
    """
    left, top, right, bottom = font.getbbox(label)
    return int(left), int(top), int(right + 0.5), int(bottom + 0.5)


def _chip(
    box: Rect, ink: tuple[int, int, int, int], image: Image.Image, placed: list[Rect]
) -> Rect:
    """Where the number for `box` goes: outside its top-left if there is room, else
    tucked into a corner of the box, preferring somewhere no other chip has been."""
    width = ink[2] - ink[0] + 2 * CHIP_PAD_X
    height = ink[3] - ink[1] + 2 * CHIP_PAD_Y
    corners = (
        (box.left, box.top - height),  # above, outside — never covers the element
        (box.left, box.top),  # inside, top-left
        (box.right - width, box.top),  # inside, top-right
        (box.left, box.bottom - height),  # inside, bottom-left
    )
    options = [_inside(Rect(x, y, x + width, y + height), image) for x, y in corners]
    free = next(
        (rect for rect in options if not any(_overlaps(rect, other) for other in placed)), None
    )
    return free if free is not None else options[0]


def _inside(chip: Rect, image: Image.Image) -> Rect:
    """`chip` nudged so it lies inside the image, keeping its size."""
    left = min(max(chip.left, 0), max(image.width - chip.width, 0))
    top = min(max(chip.top, 0), max(image.height - chip.height, 0))
    return Rect(left, top, left + chip.width, top + chip.height)


def _overlaps(a: Rect, b: Rect) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _contrast(colour: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever the digits will read as on `colour`."""
    red, green, blue = colour
    luminance = (299 * red + 587 * green + 114 * blue) // 1000
    return _BLACK if luminance > 140 else _WHITE


@lru_cache(maxsize=1)
def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Pillow's bundled face at `LABEL_FONT_PX`, or its fixed-size one if this build
    of Pillow has no FreeType. No font is read off the user's machine."""
    try:
        return ImageFont.load_default(size=LABEL_FONT_PX)
    except (OSError, ImportError):  # pragma: no cover - wheels all ship FreeType
        log.warning("mark.font_unscalable")
        return ImageFont.load_default()
