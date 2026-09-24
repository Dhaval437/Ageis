"""Grounding: an element id the model chose, turned into a pixel a click can be aimed at.

The model is shown a marked screenshot and answers with an **element id** — the
walk `id` `mark()` drew on the chip. `resolve(pruned, element_id)` turns that id
into a `Point` on the virtual desktop, and before it does, proves the element is
still the thing the model saw and still the thing a click there would reach.
The screenshot is a moment in the past: a list scrolls, a dialog opens over the
button, the window is closed, the monitor is unplugged. Every one of those makes
the observed box point at the wrong thing, and none of them raises anywhere else.

So `resolve()` never trusts the observation's rectangle. It takes a **fresh
observation** of the window and checks, in order:

1. **The screen.** The fresh walk must be under the same `DisplayLayout` the
   model's observation was (`RECOVERY.md § 3.3`: never reuse a pre-change box).
2. **The element.** It is re-found by UIA's `runtime_id`, which was stable for
   every element across walks of a 1 769-element Electron window, and must have
   the same role and the same name — both after the same redaction the observed
   tree went through. A *Play* button that has become *Pause* since the
   screenshot is the same element and the opposite click, so a changed name
   refuses. It must be enabled, on screen, and have a visible part.
3. **What is on top.** The click point is the centre of the element's **fresh**
   visible rectangle. Windows' own hit test (`WindowFromPoint`) must land in the
   element's window, and UIA's hit test at that point must land on the element,
   inside it, or — because some providers hit-test coarsely, and Chromium's
   caption buttons, *Close* among them, are only ever answered by the pane that
   holds them — on one of its ancestors and nothing below that. Any *other*
   element there is something drawn over the target, and refuses.

A refusal is a `GroundingError` whose message says which of those failed, so
the agent observes again instead of clicking. The `Grounding` it returns is
itself a snapshot: the click that uses it must still check its `layout`
(`verify_layout()`) immediately before it fires.

**Text lines** (`P2-13`). A window that draws its own content — a canvas, a
terminal, an app with accessibility off — has text OCR read (`Redacted.text`) and
no element to name. `text_targets(redacted)` lists the lines that may be offered
to the model, by their index in `Redacted.text`, and `resolve_text()` grounds the
one it picks. With no element identity to re-find, this is the coordinate path
`ARCHITECTURE.md § 6.3` calls the last resort, so the check is the pixels
themselves: a **fresh observation** of the window, walked, captured and put
through `redact()` again — the only route by which OCR ever reads the screen —
must read the **same text in the same place** (within `TEXT_PLACE_PX`). A line
that scrolled, changed, was covered or was blacked out refuses. Windows' hit
test must land in the window as well, since pixels can show through a window
that does not take the click. A line redaction blanked is never offered.
What this cannot see: an invisible element in the same window that takes
clicks over the text without drawing anything.

All coordinates are physical virtual-desktop pixels (`display.py`), so there is
no DPI correction to make: the point is already in the space `SendInput` is
driven in. Names and text are compared, never logged; the debug line carries the
id, counts and timings.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from aegis_core.perception import win32
from aegis_core.perception.display import (
    DisplayLayout,
    LayoutChangedError,
    Point,
    Rect,
    verify_layout,
)
from aegis_core.perception.ocr import SYSTEM_OCR, OcrEngine, OcrLine, blind_regions
from aegis_core.perception.prune import Candidate, PrunedTree, visible_part
from aegis_core.perception.redact import Redacted, redact, redact_tree
from aegis_core.perception.screen import WindowTarget, capture
from aegis_core.perception.uia_tree import UiaElement, UiaTree, hit_chain, walk

log = logging.getLogger(__name__)

#: The message every refusal ends with: what the agent does next.
OBSERVE_AGAIN: Final = "Take a fresh look at the screen before choosing again."

#: How far, in physical pixels, each edge of a re-read text line may be from where
#: the screenshot showed it. Measured: the same pixels read six times gave boxes
#: identical to the pixel; a line that scrolled has moved a whole line height.
TEXT_PLACE_PX: Final = 2

#: A text line smaller than this on either side is a speck OCR read, not a target.
MIN_TEXT_PX: Final = 4


class GroundingError(RuntimeError):
    """The element cannot be clicked safely right now, for the reason the message states."""


class Hit(StrEnum):
    """What UIA's hit test found at the click point, relative to the target."""

    #: The element itself.
    TARGET = "target"
    #: Something inside it: the label of a button, a cell of a row.
    INSIDE = "inside"
    #: One of its ancestors and nothing below it — a provider whose hit test
    #: stops above the element. Nothing else claims the point.
    CONTAINER = "container"


@dataclass(frozen=True, slots=True)
class Grounding:
    """Where to click to reach one element, verified a moment ago.

    `point` and `box` are physical virtual-desktop pixels measured under
    `layout`; `box` is the element's visible rectangle *now*, which is not
    necessarily where the screenshot showed it (`moved`).
    """

    element_id: int
    point: Point
    box: Rect
    hwnd: int
    layout: DisplayLayout
    moved: bool
    hit: Hit


def resolve(
    pruned: PrunedTree,
    element_id: int,
    *,
    offered: Collection[int] | None = None,
) -> Grounding:
    """The verified click point of `element_id`, one of `pruned`'s candidates.

    `element_id` is what the model answered, so it is checked like any other
    untrusted input. `offered`, when given, is the set of ids the model was
    actually shown — `Marked.ids` — and an id outside it is refused even if it is
    a candidate.

    Raises `GroundingError` when the element is not one that was offered, cannot
    be found again, has changed, cannot be clicked, or is covered;
    `LayoutChangedError` when the monitors changed since the observation; and
    whatever `walk()` raises when the window cannot be read.
    """
    started = time.perf_counter()
    candidate = _candidate(pruned, element_id, offered)
    observed = candidate.element
    tree = pruned.tree
    if not observed.runtime_id:
        raise GroundingError(
            f"Element {element_id} cannot be found again: its app gives it no identity. "
            + OBSERVE_AGAIN
        )
    _check_window(tree.hwnd)
    fresh, _ = redact_tree(walk(tree.hwnd))
    if fresh.layout != tree.layout:
        raise LayoutChangedError(
            "The display layout changed since the screenshot. A fresh observation is needed."
        )
    current = _refind(fresh, observed, element_id)
    window = fresh.elements[0].bbox if fresh.elements else None
    box = visible_part(current.bbox, window, fresh.layout)
    if box is None:
        raise GroundingError(f"Element {element_id} is no longer visible. {OBSERVE_AGAIN}")
    point = Point(box.left + box.width // 2, box.top + box.height // 2)
    hit = _on_top(fresh, current, point, element_id)
    layout = verify_layout(fresh.layout)
    grounding = Grounding(
        element_id=element_id,
        point=point,
        box=box,
        hwnd=tree.hwnd,
        layout=layout,
        moved=box != candidate.bbox,
        hit=hit,
    )
    log.debug(
        "screen.grounded",
        extra={
            "element_id": element_id,
            "hit": hit.value,
            "moved": grounding.moved,
            "elements": len(fresh.elements),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return grounding


def _candidate(pruned: PrunedTree, element_id: int, offered: Collection[int] | None) -> Candidate:
    """The candidate `element_id` names, refusing anything the model was not shown."""
    if not isinstance(element_id, int) or isinstance(element_id, bool):
        raise GroundingError(f"An element id is a whole number. {OBSERVE_AGAIN}")
    if offered is not None and element_id not in offered:
        raise GroundingError(f"Element {element_id} is not one of the marked elements.")
    found = next((c for c in pruned.candidates if c.id == element_id), None)
    if found is None:
        raise GroundingError(f"Element {element_id} is not one of the marked elements.")
    return found


def _check_window(hwnd: int) -> None:
    """Refuse a window that cannot be clicked into, with its own reason."""
    if not win32.is_window(hwnd):
        raise GroundingError(f"That window has closed. {OBSERVE_AGAIN}")
    if win32.is_minimised(hwnd):
        raise GroundingError(f"That window is minimised. {OBSERVE_AGAIN}")
    if win32.is_cloaked(hwnd):
        raise GroundingError(f"That window is on another virtual desktop. {OBSERVE_AGAIN}")


def _refind(fresh: UiaTree, observed: UiaElement, element_id: int) -> UiaElement:
    """The element in `fresh` that is `observed`, or a refusal saying why there is none."""
    matches = [e for e in fresh.elements if e.runtime_id == observed.runtime_id]
    if not matches:
        raise GroundingError(f"Element {element_id} is no longer on screen. {OBSERVE_AGAIN}")
    if len(matches) > 1:
        raise GroundingError(
            f"Element {element_id} cannot be told apart from another element. {OBSERVE_AGAIN}"
        )
    current = matches[0]
    if current.role != observed.role or current.name != observed.name:
        raise GroundingError(
            f"Element {element_id} has changed since the screenshot. {OBSERVE_AGAIN}"
        )
    if not current.enabled:
        raise GroundingError(f"Element {element_id} is disabled, so a click would do nothing.")
    if current.offscreen:
        raise GroundingError(f"Element {element_id} has scrolled out of view. {OBSERVE_AGAIN}")
    return current


def _on_top(fresh: UiaTree, target: UiaElement, point: Point, element_id: int) -> Hit:
    """How the target is reached at `point`, or a refusal naming what covers it."""
    if win32.root_window_at(point.x, point.y) != fresh.hwnd:
        raise GroundingError(f"Another window is covering element {element_id}. {OBSERVE_AGAIN}")
    ancestors = _ancestor_ids(fresh, target)
    chain = hit_chain(point, {target.runtime_id} | ancestors)
    if chain and chain[-1] == target.runtime_id:
        return Hit.TARGET if len(chain) == 1 else Hit.INSIDE
    if len(chain) == 1 and chain[0] in ancestors:
        return Hit.CONTAINER
    raise GroundingError(
        f"Something else is drawn over element {element_id} at that point. {OBSERVE_AGAIN}"
    )


def _ancestor_ids(tree: UiaTree, element: UiaElement) -> set[tuple[int, ...]]:
    """The runtime ids of every element `element` sits inside, up to the window."""
    ids: set[tuple[int, ...]] = set()
    parent = element.parent
    while parent is not None:
        above = tree.elements[parent]
        if above.runtime_id:
            ids.add(above.runtime_id)
        parent = above.parent
    return ids


# --------------------------------------------------------------------------- #
# Text lines read by OCR
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TextTarget:
    """A line of OCR text that may be offered to the model as something to click.

    `id` is the line's index in `Redacted.text`. `text` is untrusted screen text,
    already redacted, and kept out of `repr`. `hwnd` is the walked window whose
    blind region the line was read from.
    """

    id: int
    text: str = field(repr=False)
    box: Rect
    hwnd: int


@dataclass(frozen=True, slots=True)
class TextGrounding:
    """Where to click to reach one line of text, verified a moment ago.

    The same shape as `Grounding` where a click needs it — `point`, `box`, `hwnd`,
    `layout` — and the click must re-check `layout` the same way.
    """

    line_id: int
    point: Point
    box: Rect
    hwnd: int
    layout: DisplayLayout
    moved: bool


def text_targets(redacted: Redacted) -> tuple[TextTarget, ...]:
    """The lines of `redacted.text` a model may be offered as click targets.

    Left out: a line redaction blanked, a blank or speck-sized one, and one that
    cannot be put down to exactly one walked window's blind region — two walked
    windows overlapping — since grounding has to know which window to re-read.
    """
    regions = [(tree.hwnd, blind_regions(tree)) for tree in redacted.trees]
    targets: list[TextTarget] = []
    for index, line in enumerate(redacted.text):
        if not _offerable(line):
            continue
        owners = {hwnd for hwnd, rects in regions if any(r.contains_rect(line.bbox) for r in rects)}
        if len(owners) == 1:
            targets.append(TextTarget(id=index, text=line.text, box=line.bbox, hwnd=owners.pop()))
    return tuple(targets)


def resolve_text(
    observed: Redacted,
    line_id: int,
    *,
    offered: Collection[int] | None = None,
    ocr: OcrEngine = SYSTEM_OCR,
) -> TextGrounding:
    """The verified click point of text line `line_id` of `observed`.

    `line_id` is what the model answered, checked like any untrusted input;
    `offered`, when given, is the set of line ids it was shown. The window is
    observed again — walked, captured, redacted and read by `ocr` — and the line
    must be read there with the same text, in the same place.

    Raises `GroundingError` when the line was not offered, cannot be read again,
    has changed or moved, or is covered by another window; `LayoutChangedError`
    when the monitors changed; and whatever `walk()`, `capture()` or `redact()`
    raise when the window cannot be observed.
    """
    started = time.perf_counter()
    target = _text_target(observed, line_id, offered)
    _check_window(target.hwnd)
    tree = walk(target.hwnd)
    if tree.layout != observed.frame.layout:
        raise LayoutChangedError(
            "The display layout changed since the screenshot. A fresh observation is needed."
        )
    fresh = redact(capture(WindowTarget(target.hwnd)), [tree], ocr=ocr)
    if any(_overlaps(rect, target.box) for rect in fresh.unread):
        raise GroundingError(f"Text line {line_id} could not be read again. {OBSERVE_AGAIN}")
    matches = [
        line
        for line in fresh.text
        if _offerable(line) and line.text == target.text and _near(line.bbox, target.box)
    ]
    if not matches:
        raise GroundingError(
            f"Text line {line_id} has changed or moved since the screenshot. {OBSERVE_AGAIN}"
        )
    if len(matches) > 1:
        raise GroundingError(
            f"Text line {line_id} cannot be told apart from another line. {OBSERVE_AGAIN}"
        )
    box = matches[0].bbox
    point = Point(box.left + box.width // 2, box.top + box.height // 2)
    if win32.root_window_at(point.x, point.y) != target.hwnd:
        raise GroundingError(f"Another window is covering text line {line_id}. {OBSERVE_AGAIN}")
    grounding = TextGrounding(
        line_id=line_id,
        point=point,
        box=box,
        hwnd=target.hwnd,
        layout=verify_layout(tree.layout),
        moved=box != target.box,
    )
    log.debug(
        "screen.text_grounded",
        extra={
            "line_id": line_id,
            "lines": len(fresh.text),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return grounding


def _text_target(observed: Redacted, line_id: int, offered: Collection[int] | None) -> TextTarget:
    """The offerable line `line_id` names, refusing anything the model was not shown."""
    if not isinstance(line_id, int) or isinstance(line_id, bool):
        raise GroundingError(f"A text line id is a whole number. {OBSERVE_AGAIN}")
    if offered is not None and line_id not in offered:
        raise GroundingError(f"Text line {line_id} is not one of the offered lines.")
    found = next((t for t in text_targets(observed) if t.id == line_id), None)
    if found is None:
        raise GroundingError(f"Text line {line_id} is not one of the offered lines.")
    return found


def _offerable(line: OcrLine) -> bool:
    box = line.bbox
    return (
        not line.redacted
        and bool(line.text.strip())
        and box.width >= MIN_TEXT_PX
        and box.height >= MIN_TEXT_PX
    )


def _near(a: Rect, b: Rect) -> bool:
    return (
        abs(a.left - b.left) <= TEXT_PLACE_PX
        and abs(a.top - b.top) <= TEXT_PLACE_PX
        and abs(a.right - b.right) <= TEXT_PLACE_PX
        and abs(a.bottom - b.bottom) <= TEXT_PLACE_PX
    )


def _overlaps(a: Rect, b: Rect) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom
