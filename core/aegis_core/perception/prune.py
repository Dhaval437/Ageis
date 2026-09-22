"""A walked UI tree cut down to the elements worth showing a model, best first.

`walk()` returns everything in a window's control view — 808 elements for a VS Code
editor, 243 for an Explorer folder — and most of it is noise: scrolled-out rows, unnamed
layout panes, the icon inside every toolbar button, a label repeating the button
it sits in. `prune()` removes that, then ranks what is left and keeps at most
`MAX_CANDIDATES`, so the planner's context holds the controls a person would
actually look at. It is the biggest single lever on grounding quality: an element
that is not in this list cannot be clicked by id.

Two stages, both pure:

1. **Pruning** drops what carries nothing. An element is gone when it is
   offscreen, has no area, or has no pixel inside both the window and a monitor;
   when it is not interactive and has neither a name nor a value (a layout
   wrapper, a decorative icon); and when its text only repeats the element it
   sits in (`Text 'Save'` inside `Button 'Save'`, the *Name* cell of a file row,
   a `Group 'Close'` wrapped round `Button 'Close'`).
2. **Ranking** scores the survivors — interactive over text over structure,
   named over unnamed, enabled over disabled, a cell or a scrollbar part below
   the thing it belongs to, and anything whose name shares a word with the task
   goal above all of those — and keeps the best `limit`. The focused element and
   the window itself are always kept.

The candidates come back in **document order**, not score order: a model reads a
layout top to bottom, and a list shuffled by score reads as nonsense. Each keeps
its `UiaElement` — and so its walk `id` and `runtime_id` — for `P2-08` to resolve,
and its `bbox` is the **visible** part, which is where a click can land.

Ancestors do not clip their children. Offscreen already covers scrolled-out
content, and a container whose rectangle is smaller than what it holds is common
enough in real providers that clipping to it would hide visible controls.

Every name here is untrusted screen text. It is only compared and scored, never
logged — the debug line carries counts and timings.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Final

from aegis_core.perception.display import DisplayLayout, Rect
from aegis_core.perception.uia_tree import MAX_ELEMENTS, UiaElement, UiaTree

log = logging.getLogger(__name__)

#: The most elements a model is shown from one window (`ARCHITECTURE.md § 6.3`).
MAX_CANDIDATES: Final = 200

#: Roles a person acts on directly. An unnamed one is still kept: an icon button
#: has no text, and the model can see it in the screenshot.
INTERACTIVE_ROLES: Final = frozenset({
    "Button", "Calendar", "CheckBox", "ComboBox", "DataItem", "Edit", "HeaderItem",
    "Hyperlink", "ListItem", "MenuItem", "RadioButton", "Slider", "Spinner",
    "SplitButton", "TabItem", "TreeItem",
})  # fmt: skip

#: Roles that are read rather than acted on.
TEXT_ROLES: Final = frozenset({
    "Document", "Header", "Image", "ProgressBar", "StatusBar", "Text", "TitleBar", "ToolTip",
})  # fmt: skip

#: A row of a list, grid or tree. What sits inside one is a detail of it.
ITEM_ROLES: Final = frozenset({"DataItem", "ListItem", "TreeItem"})

# Scores. Only their order matters; the gaps say which signal outranks which.
SCORE_INTERACTIVE: Final = 100
SCORE_TEXT: Final = 50
SCORE_STRUCTURE: Final = 20
BONUS_NAME: Final = 20
BONUS_VALUE: Final = 10
#: Per goal word the name contains, up to `MAX_GOAL_WORDS_SCORED` of them — so a
#: matching label outranks an unmatched button.
BONUS_GOAL_WORD: Final = 60
MAX_GOAL_WORDS_SCORED: Final = 2
PENALTY_DISABLED: Final = 40
#: A cell of a row: its details, below the row itself.
PENALTY_IN_ITEM: Final = 30
#: `Line down`, `Page up`: the scroll tool does this better than a click.
PENALTY_IN_SCROLLBAR: Final = 80
PENALTY_TINY: Final = 40
#: Physical pixels. Narrower than this on either side is barely a target.
TINY_PX: Final = 8

#: A name longer than this many words is prose, not a label, and earns no goal
#: bonus: measured in an editor, transcript lines that happened to contain "run"
#: and "tests" outranked the *Run* menu for the goal "run the tests".
MAX_LABEL_WORDS: Final = 12
#: At most this many words of the goal are matched against names.
MAX_GOAL_WORDS: Final = 32
#: Words too common to say anything about which element a goal means.
STOP_WORDS: Final = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "it", "its", "of",
    "on", "or", "the", "then", "this", "to", "with",
})  # fmt: skip

_WORD = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One element a model may be shown, and why it ranked where it did.

    `bbox` is the part of the element that is on screen, inside its window.
    `parent` is the `id` of the nearest ancestor that is also a candidate.
    """

    element: UiaElement
    bbox: Rect
    parent: int | None
    score: int

    @property
    def id(self) -> int:
        return self.element.id


@dataclass(frozen=True, slots=True)
class PrunedTree:
    """The candidates of one walk, in document order, and what the cap left out."""

    tree: UiaTree
    candidates: tuple[Candidate, ...]
    #: Elements that survived pruning but ranked below the cap.
    omitted: int

    @property
    def truncated(self) -> bool:
        """True if anything visible and meaningful is missing from `candidates`."""
        return self.omitted > 0 or self.tree.truncated


def prune(tree: UiaTree, *, goal: str = "", limit: int = MAX_CANDIDATES) -> PrunedTree:
    """The at most `limit` elements of `tree` most worth showing, in document order.

    `goal` is the task in the user's words; an element whose name shares a word
    with it ranks higher. Raises `ValueError` for a `limit` outside
    `1..MAX_ELEMENTS`.
    """
    if not 1 <= limit <= MAX_ELEMENTS:
        raise ValueError(f"limit must be between 1 and {MAX_ELEMENTS}, not {limit}.")
    started = time.perf_counter()
    survivors = _survivors(tree, _goal_words(goal))
    ranked = sorted(survivors.values(), key=lambda s: (not s.pinned, -s.score, s.element.id))
    chosen = sorted(ranked[:limit], key=lambda s: s.element.id)
    kept = {entry.element.id for entry in chosen}
    candidates = tuple(
        Candidate(
            element=entry.element,
            bbox=entry.visible,
            parent=_nearest(entry.element.parent, kept, tree.elements),
            score=entry.score,
        )
        for entry in chosen
    )
    log.debug(
        "uia.pruned",
        extra={
            "elements": len(tree.elements),
            "survivors": len(survivors),
            "candidates": len(candidates),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return PrunedTree(tree=tree, candidates=candidates, omitted=len(survivors) - len(candidates))


def focused_element(elements: tuple[UiaElement, ...]) -> int | None:
    """The `id` of the element that has keyboard focus, or `None`.

    Providers do not agree that only one element has it: in Notepad, three
    unnamed XAML host panes all report focus at once. So the answer is the last
    claimant in document order — the innermost — that is something a person
    could be typing into or reading: an interactive or text role, or a name.
    """
    return next(
        (
            e.id
            for e in reversed(elements)
            if e.focused and (e.role in INTERACTIVE_ROLES or e.role in TEXT_ROLES or e.name)
        ),
        None,
    )


def visible_part(bbox: Rect | None, window: Rect | None, layout: DisplayLayout) -> Rect | None:
    """The part of `bbox` inside `window` and on a monitor, or `None` if there is none.

    When the element straddles monitors, the larger piece is kept: it is the
    one a click is most likely to reach, and a dead zone may lie between them.
    """
    if bbox is None:
        return None
    if window is not None:
        clipped = _intersect(bbox, window)
        if clipped is None:
            return None
        bbox = clipped
    pieces = (_intersect(bbox, monitor.bounds) for monitor in layout.monitors)
    return max(
        (piece for piece in pieces if piece is not None),
        key=lambda r: r.width * r.height,
        default=None,
    )


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Entry:
    """An element that survived pruning: where it is visible and how it ranks."""

    element: UiaElement
    visible: Rect
    score: int
    #: The window itself and the focused element: kept whatever they score.
    pinned: bool
    interactive: bool
    textual: bool


def _survivors(tree: UiaTree, goal_words: frozenset[str]) -> dict[int, _Entry]:
    """Every element worth ranking, by `id`.

    Walks in document order, so an element's ancestors have always been decided
    before it is. `nearest[id]` is the closest surviving ancestor-or-self, which
    is what "the element it sits in" means once wrappers have been removed.
    """
    elements = tree.elements
    window = elements[0].bbox if elements else None
    focus = focused_element(elements)
    survivors: dict[int, _Entry] = {}
    nearest: dict[int, int | None] = {}
    in_item: dict[int, bool] = {}
    in_scrollbar: dict[int, bool] = {}
    for e in elements:
        parent = e.parent
        up = nearest[parent] if parent is not None else None
        inside_item = parent is not None and (
            in_item[parent] or elements[parent].role in ITEM_ROLES
        )
        inside_scrollbar = parent is not None and (
            in_scrollbar[parent] or elements[parent].role == "ScrollBar"
        )
        in_item[e.id] = inside_item
        in_scrollbar[e.id] = inside_scrollbar
        nearest[e.id] = up
        visible = None if e.offscreen else visible_part(e.bbox, window, tree.layout)
        if visible is None:
            continue
        interactive = e.role in INTERACTIVE_ROLES
        textual = e.role in TEXT_ROLES
        pinned = e.id == 0 or e.id == focus
        if not pinned:
            if not interactive and not e.name and not e.value:
                continue
            if up is not None and _repeats(e, interactive, elements[up], inside_item):
                continue
        score = _score(
            e,
            interactive=interactive,
            textual=textual,
            in_item=inside_item and e.role not in ITEM_ROLES,
            in_scrollbar=inside_scrollbar,
            tiny=visible.width < TINY_PX or visible.height < TINY_PX,
            goal_words=goal_words,
        )
        survivors[e.id] = _Entry(e, visible, score, pinned, interactive, textual)
        nearest[e.id] = e.id
    _drop_wrappers(survivors, nearest, elements)
    return survivors


def _score(
    e: UiaElement,
    *,
    interactive: bool,
    textual: bool,
    in_item: bool,
    in_scrollbar: bool,
    tiny: bool,
    goal_words: frozenset[str],
) -> int:
    if interactive:
        score = SCORE_INTERACTIVE - (0 if e.enabled else PENALTY_DISABLED)
    elif textual:
        score = SCORE_TEXT
    else:
        score = SCORE_STRUCTURE
    if e.name:
        score += BONUS_NAME
    if e.value:
        score += BONUS_VALUE
    if in_item:
        score -= PENALTY_IN_ITEM
    if in_scrollbar:
        score -= PENALTY_IN_SCROLLBAR
    if tiny:
        score -= PENALTY_TINY
    if goal_words and e.name:
        words = _WORD.findall(e.name.casefold())
        if len(words) <= MAX_LABEL_WORDS:
            matched = len(goal_words.intersection(words))
            score += BONUS_GOAL_WORD * min(matched, MAX_GOAL_WORDS_SCORED)
    return score


def _repeats(e: UiaElement, interactive: bool, holder: UiaElement, inside_item: bool) -> bool:
    """True if `e` says nothing its surviving ancestor `holder` does not.

    A label inside a control repeats the control's name. A cell of a row may be
    interactive, but the one whose value is the row's name is still the row.
    """
    if not holder.name:
        return False
    if not interactive and e.name == holder.name:
        return True
    return inside_item and e.value == holder.name and holder.role in ITEM_ROLES


def _drop_wrappers(
    survivors: dict[int, _Entry],
    nearest: dict[int, int | None],
    elements: tuple[UiaElement, ...],
) -> None:
    """Remove a structural element named exactly like an interactive one directly inside it.

    `Group 'Close'` round `Button 'Close'` is one thing on screen; the button is
    the part that can be clicked. Only the wrapper goes, so this runs after the
    children have all been seen.
    """
    wrappers: set[int] = set()
    for entry in survivors.values():
        e = entry.element
        if not entry.interactive or not e.name or e.parent is None:
            continue
        holder = nearest.get(e.parent)
        if holder is None or holder not in survivors:
            continue
        wrapper = survivors[holder]
        if wrapper.interactive or wrapper.textual or wrapper.pinned:
            continue
        if elements[holder].name == e.name:
            wrappers.add(holder)
    for holder in wrappers:
        del survivors[holder]


def _nearest(parent: int | None, kept: set[int], elements: tuple[UiaElement, ...]) -> int | None:
    """The closest ancestor, starting at `parent`, that is in `kept`."""
    while parent is not None and parent not in kept:
        parent = elements[parent].parent
    return parent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _intersect(a: Rect, b: Rect) -> Rect | None:
    rect = Rect(
        max(a.left, b.left), max(a.top, b.top), min(a.right, b.right), min(a.bottom, b.bottom)
    )
    return None if rect.is_empty else rect


def _goal_words(goal: str) -> frozenset[str]:
    """The distinct meaningful words of `goal`, the first `MAX_GOAL_WORDS` of them."""
    words: list[str] = []
    for word in _WORD.findall(goal.casefold()):
        if len(word) > 1 and word not in STOP_WORDS and word not in words:
            words.append(word)
            if len(words) == MAX_GOAL_WORDS:
                break
    return frozenset(words)
