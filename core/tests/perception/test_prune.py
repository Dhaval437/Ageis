"""Tests for `perception/prune.py` — cutting a walked tree down to what a model is shown.

Pure trees built by hand for every rule — each shaped like something measured on
a real window (an Explorer file row, a toolbar button with an icon inside, an
editor's wrapper groups, Notepad's three "focused" panes) — then one live walk of
a real window this test opens.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows UI Automation")

from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.prune import (  # noqa: E402
    MAX_CANDIDATES,
    MAX_LABEL_WORDS,
    PrunedTree,
    focused_element,
    prune,
    visible_part,
)
from aegis_core.perception.uia_tree import MAX_ELEMENTS, UiaElement, UiaTree, walk  # noqa: E402

from tests.perception.helpers import FormWindow  # noqa: E402


def _monitor(left: int, width: int, *, primary: bool) -> Monitor:
    bounds = Rect(left, 0, left + width, 1080)
    return Monitor(device=f"D{left}", bounds=bounds, work_area=bounds, dpi=96, primary=primary)


#: Two monitors with a dead zone between them: nothing shows x 1920..1999.
DESK = DisplayLayout(
    monitors=(_monitor(0, 1920, primary=True), _monitor(2000, 1920, primary=False)),
    virtual=Rect(0, 0, 3920, 1080),
)
WINDOW = Rect(100, 100, 1100, 900)


class Tree:
    """Builds a `UiaTree` element by element, in document order."""

    def __init__(self, window: Rect | None = WINDOW, name: str = "App") -> None:
        self.elements: list[UiaElement] = []
        self.root = self.add(None, "Window", name, bbox=window)

    def add(
        self,
        parent: int | None,
        role: str,
        name: str = "",
        *,
        value: str | None = None,
        bbox: Rect | None = None,
        enabled: bool = True,
        focused: bool = False,
        offscreen: bool = False,
        is_password: bool = False,
    ) -> int:
        index = len(self.elements)
        depth = 0 if parent is None else self.elements[parent].depth + 1
        if bbox is None and parent is not None:
            bbox = Rect(200, 200 + 10 * index, 400, 230 + 10 * index)
        self.elements.append(
            UiaElement(
                id=index,
                parent=parent,
                depth=depth,
                role=role,
                name=name,
                value=value,
                bbox=bbox,
                enabled=enabled,
                focused=focused,
                offscreen=offscreen,
                is_password=is_password,
                automation_id="",
                class_name="",
                runtime_id=(42, index),
            )
        )
        return index

    def build(self, *, truncated: bool = False) -> UiaTree:
        return UiaTree(
            hwnd=1,
            elements=tuple(self.elements),
            truncated=truncated,
            layout=DESK,
            captured_at=0.0,
        )

    def prune(self, **kwargs: object) -> PrunedTree:
        return prune(self.build(), **kwargs)  # type: ignore[arg-type]


def ids(pruned: PrunedTree) -> list[int]:
    return [c.id for c in pruned.candidates]


def score_of(pruned: PrunedTree, element_id: int) -> int:
    return next(c.score for c in pruned.candidates if c.id == element_id)


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #


def test_offscreen_and_arealess_elements_are_dropped() -> None:
    t = Tree()
    shown = t.add(t.root, "Button", "Shown")
    t.add(t.root, "Button", "Scrolled away", offscreen=True)
    arealess = t.add(t.root, "Button", "No area")
    t.elements[arealess] = replace(t.elements[arealess], bbox=None)
    assert ids(t.prune()) == [t.root, shown]


def test_an_element_outside_its_window_is_dropped_and_one_across_its_edge_is_clipped() -> None:
    t = Tree()
    t.add(t.root, "Button", "Outside", bbox=Rect(1200, 200, 1300, 240))
    across = t.add(t.root, "Button", "Across", bbox=Rect(1000, 200, 1300, 240))
    pruned = t.prune()
    assert ids(pruned) == [t.root, across]
    assert pruned.candidates[1].bbox == Rect(1000, 200, 1100, 240)


def test_an_element_in_a_dead_zone_between_monitors_is_dropped() -> None:
    t = Tree(window=Rect(1800, 100, 2200, 900))
    t.add(t.root, "Button", "In the gap", bbox=Rect(1930, 200, 1990, 240))
    kept = t.add(t.root, "Button", "On the second monitor", bbox=Rect(2050, 200, 2100, 240))
    assert ids(t.prune()) == [t.root, kept]


def test_an_element_across_two_monitors_keeps_its_larger_visible_piece() -> None:
    assert visible_part(Rect(1900, 0, 2100, 50), None, DESK) == Rect(2000, 0, 2100, 50)
    assert visible_part(Rect(1800, 0, 2010, 50), None, DESK) == Rect(1800, 0, 1920, 50)


def test_visible_part_without_a_window_still_needs_a_monitor() -> None:
    assert visible_part(Rect(-50, 0, -10, 50), None, DESK) is None
    assert visible_part(None, WINDOW, DESK) is None


def test_a_window_that_is_not_on_screen_yields_nothing() -> None:
    t = Tree(window=Rect(-32000, -32000, -31800, -31900))  # where Windows parks a minimised one
    t.add(t.root, "Button", "Save", bbox=Rect(-31990, -31990, -31900, -31950))
    pruned = t.prune()
    assert pruned.candidates == ()
    assert pruned.omitted == 0


def test_an_empty_tree_prunes_to_nothing() -> None:
    tree = UiaTree(hwnd=1, elements=(), truncated=False, layout=DESK, captured_at=0.0)
    assert prune(tree).candidates == ()


# --------------------------------------------------------------------------- #
# Pruning what carries nothing
# --------------------------------------------------------------------------- #


def test_unnamed_wrappers_go_and_their_children_attach_to_the_nearest_kept_ancestor() -> None:
    t = Tree()
    pane = t.add(t.root, "Pane")
    group = t.add(pane, "Group")
    button = t.add(group, "Button", "Save")
    pruned = t.prune()
    assert ids(pruned) == [t.root, button]
    assert pruned.candidates[1].parent == t.root


def test_an_unnamed_icon_or_label_goes_but_an_unnamed_button_stays() -> None:
    t = Tree()
    icon_button = t.add(t.root, "Button")
    t.add(icon_button, "Image")
    t.add(t.root, "Text")
    assert ids(t.prune()) == [t.root, icon_button]


def test_a_text_element_with_a_value_but_no_name_is_kept() -> None:
    t = Tree()
    status = t.add(t.root, "Text", value="3 items selected")
    assert status in ids(t.prune())


def test_a_label_repeating_the_control_it_sits_in_goes() -> None:
    # Explorer's breadcrumb: SplitButton 'Windows' > Text 'Windows'.
    t = Tree()
    crumb = t.add(t.root, "SplitButton", "Windows")
    t.add(crumb, "Text", "Windows")
    hint = t.add(crumb, "Text", "Go to Windows")
    assert ids(t.prune()) == [t.root, crumb, hint]


def test_a_repeat_is_judged_against_the_nearest_kept_ancestor_not_the_raw_parent() -> None:
    t = Tree()
    button = t.add(t.root, "Button", "Open")
    wrapper = t.add(button, "Group")
    t.add(wrapper, "Text", "Open")
    pruned = t.prune()
    assert ids(pruned) == [t.root, button]


def test_an_interactive_child_is_never_dropped_for_repeating_its_parent() -> None:
    t = Tree()
    item = t.add(t.root, "MenuItem", "View")
    inner = t.add(item, "MenuItem", "View")
    assert ids(t.prune()) == [t.root, item, inner]


def test_a_file_rows_name_cell_goes_and_its_other_cells_rank_below_the_row() -> None:
    # Explorer's details view: ListItem '1028' > Edit 'Name' = '1028', Edit 'Type' = …
    t = Tree()
    row = t.add(t.root, "ListItem", "1028", value="1028")
    t.add(row, "Edit", "Name", value="1028")
    kind = t.add(row, "Edit", "Type", value="File folder")
    pruned = t.prune()
    assert ids(pruned) == [t.root, row, kind]
    assert score_of(pruned, kind) < score_of(pruned, row)
    assert pruned.candidates[2].parent == row


def test_a_structural_wrapper_named_like_the_button_inside_it_goes() -> None:
    # An editor's toolbar: Group 'Close' > Button 'Close'.
    t = Tree()
    wrapper = t.add(t.root, "Group", "Close")
    button = t.add(wrapper, "Button", "Close")
    labelled = t.add(t.root, "Group", "Recent files")
    entry = t.add(labelled, "Button", "notes.txt")
    pruned = t.prune()
    assert ids(pruned) == [t.root, button, labelled, entry]
    assert pruned.candidates[1].parent == t.root


def test_the_window_is_never_dropped_as_a_wrapper() -> None:
    t = Tree(name="Save")
    t.add(t.root, "Button", "Save")
    assert t.root in ids(t.prune())


def test_a_password_field_survives_with_its_flag_so_the_agent_knows_to_stop() -> None:
    t = Tree()
    field = t.add(t.root, "Edit", "Password", is_password=True)
    candidate = next(c for c in t.prune().candidates if c.id == field)
    assert candidate.element.is_password
    assert candidate.element.value is None


# --------------------------------------------------------------------------- #
# Focus
# --------------------------------------------------------------------------- #


def test_the_focused_element_is_the_innermost_meaningful_claimant() -> None:
    # Notepad: three unnamed XAML host panes report focus at once.
    t = Tree()
    host = t.add(t.root, "Pane", focused=True)
    editor = t.add(host, "Document", "Text editor", focused=True)
    t.add(t.root, "Pane", focused=True)
    elements = t.build().elements
    assert focused_element(elements) == editor


def test_of_two_meaningful_focus_claimants_the_inner_one_wins() -> None:
    t = Tree()
    form = t.add(t.root, "Group", "Sign-up form", focused=True)
    field = t.add(form, "Edit", "Email", focused=True)
    assert focused_element(t.build().elements) == field


def test_no_focus_claimant_worth_the_name_means_no_focused_element() -> None:
    t = Tree()
    t.add(t.root, "Pane", focused=True)
    t.add(t.root, "Button", "Save")
    assert focused_element(t.build().elements) is None


def test_meaningless_focus_claimants_are_pruned_like_anything_else() -> None:
    t = Tree()
    t.add(t.root, "Pane", focused=True)
    t.add(t.root, "Pane", focused=True)
    button = t.add(t.root, "Button", "Save")
    assert ids(t.prune()) == [t.root, button]


def test_the_window_and_the_focused_element_are_kept_whatever_they_score() -> None:
    t = Tree()
    for i in range(10):
        t.add(t.root, "Button", f"Button {i}")
    focused = t.add(t.root, "ScrollBar", "Vertical", focused=True)
    pruned = t.prune(limit=2)
    assert ids(pruned) == [t.root, focused]
    assert pruned.omitted == 10


def test_a_focused_label_repeating_its_control_is_still_kept() -> None:
    t = Tree()
    button = t.add(t.root, "Button", "Save")
    label = t.add(button, "Text", "Save", focused=True)
    assert ids(t.prune()) == [t.root, button, label]


# --------------------------------------------------------------------------- #
# Ranking and the cap
# --------------------------------------------------------------------------- #


def test_interactive_outranks_text_which_outranks_structure() -> None:
    t = Tree()
    group = t.add(t.root, "Group", "Toolbar")
    text = t.add(t.root, "Text", "Ready")
    button = t.add(t.root, "Button", "Save")
    pruned = t.prune()
    assert score_of(pruned, button) > score_of(pruned, text) > score_of(pruned, group)


def test_a_disabled_control_ranks_below_an_enabled_one_but_above_text() -> None:
    t = Tree()
    enabled = t.add(t.root, "Button", "Save")
    disabled = t.add(t.root, "Button", "Undo", enabled=False)
    text = t.add(t.root, "Text", "Ready")
    pruned = t.prune()
    assert score_of(pruned, enabled) > score_of(pruned, disabled) > score_of(pruned, text)


def test_scrollbar_parts_rank_below_plain_text() -> None:
    t = Tree()
    bar = t.add(t.root, "ScrollBar", "Vertical")
    page_down = t.add(bar, "Button", "Page down")
    text = t.add(t.root, "Text", "Ready")
    pruned = t.prune()
    assert score_of(pruned, page_down) < score_of(pruned, text)


def test_a_sliver_ranks_below_a_real_target() -> None:
    t = Tree()
    sliver = t.add(t.root, "Button", "Splitter", bbox=Rect(200, 200, 204, 600))
    button = t.add(t.root, "Button", "Save")
    pruned = t.prune()
    assert score_of(pruned, sliver) < score_of(pruned, button)


def test_the_cap_keeps_the_best_and_returns_them_in_document_order() -> None:
    t = Tree()
    texts = [t.add(t.root, "Text", f"Label {i}") for i in range(5)]
    buttons = [t.add(t.root, "Button", f"Button {i}") for i in range(3)]
    pruned = t.prune(limit=4)
    assert ids(pruned) == [t.root, *buttons]
    assert pruned.omitted == len(texts)
    assert pruned.truncated


def test_ties_at_the_cap_go_to_the_earlier_element() -> None:
    t = Tree()
    first, second, _third = (t.add(t.root, "Button", f"B{i}") for i in range(3))
    assert ids(t.prune(limit=3)) == [t.root, first, second]


def test_the_default_cap_is_two_hundred() -> None:
    t = Tree()
    for i in range(MAX_CANDIDATES + 50):
        t.add(t.root, "Button", f"Button {i}", bbox=Rect(200, 200, 400, 230))
    pruned = t.prune()
    assert len(pruned.candidates) == MAX_CANDIDATES
    assert pruned.omitted == 51  # 250 buttons + the window, less 200


def test_nothing_omitted_is_not_truncated_unless_the_walk_was() -> None:
    t = Tree()
    t.add(t.root, "Button", "Save")
    assert not t.prune().truncated
    assert prune(t.build(truncated=True)).truncated


def test_a_candidates_parent_skips_ancestors_the_cap_cut() -> None:
    t = Tree()
    group = t.add(t.root, "Group", "Sidebar")
    button = t.add(group, "Button", "Open")
    pruned = t.prune(limit=2)
    assert ids(pruned) == [t.root, button]
    assert pruned.candidates[1].parent == t.root


@pytest.mark.parametrize("limit", [0, -1, MAX_ELEMENTS + 1])
def test_a_limit_out_of_range_is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        Tree().prune(limit=limit)


# --------------------------------------------------------------------------- #
# The goal
# --------------------------------------------------------------------------- #


def test_a_control_named_in_the_goal_survives_a_cap_it_would_otherwise_miss() -> None:
    t = Tree()
    for i in range(10):
        t.add(t.root, "Button", f"Tool {i}")
    rename = t.add(t.root, "Button", "Rename")
    assert rename not in ids(t.prune(limit=5))
    assert rename in ids(t.prune(goal="Rename the report", limit=5))


def test_a_label_named_in_the_goal_outranks_an_unrelated_button() -> None:
    t = Tree()
    button = t.add(t.root, "Button", "Share")
    row = t.add(t.root, "Text", "invoice-march.pdf")
    pruned = t.prune(goal="open invoice-march.pdf")
    assert score_of(pruned, row) > score_of(pruned, button)


def test_goal_matching_ignores_case_and_stop_words() -> None:
    t = Tree()
    save = t.add(t.root, "Button", "SAVE")
    the = t.add(t.root, "Button", "The")
    pruned = t.prune(goal="save the file")
    baseline = t.prune()
    assert score_of(pruned, save) > score_of(baseline, save)
    assert score_of(pruned, the) == score_of(baseline, the)


def test_goal_matching_works_on_non_latin_words() -> None:
    t = Tree()
    button = t.add(t.root, "Button", "Speichern unter")
    assert score_of(t.prune(goal="speichern"), button) > score_of(t.prune(), button)


def test_prose_earns_no_goal_bonus_however_many_words_it_shares() -> None:
    t = Tree()
    prose = " ".join(["run the tests again"] * 4)
    assert len(prose.split()) > MAX_LABEL_WORDS
    line = t.add(t.root, "Text", prose)
    menu = t.add(t.root, "MenuItem", "Run")
    pruned = t.prune(goal="run the tests")
    assert score_of(pruned, line) == score_of(t.prune(), line)
    assert score_of(pruned, menu) > score_of(pruned, line)


def test_a_huge_goal_is_bounded() -> None:
    t = Tree()
    button = t.add(t.root, "Button", "zebra")
    goal = " ".join(f"word{i}" for i in range(10_000)) + " zebra"
    assert score_of(t.prune(goal=goal), button) == score_of(t.prune(), button)


# --------------------------------------------------------------------------- #
# Hygiene and cost
# --------------------------------------------------------------------------- #


def test_no_screen_text_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    t = Tree(name="Secret window title")
    t.add(t.root, "Edit", "Account number", value="GB29 NWBK 6016 1331 9268 19")
    with caplog.at_level(logging.DEBUG, logger="aegis_core.perception.prune"):
        t.prune(goal="type the account number")
    assert caplog.records, "prune should log its counts"
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__}"
        for secret in ("Secret window title", "Account number", "GB29", "account"):
            assert secret not in text


def test_a_tree_at_the_walk_cap_prunes_well_inside_the_observation_budget() -> None:
    t = Tree()
    parent = t.root
    for i in range(MAX_ELEMENTS - 1):
        parent = t.root if i % 50 == 0 else parent
        role = ("Group", "Button", "Text", "ListItem", "Edit")[i % 5]
        added = t.add(parent, role, f"Element {i}", bbox=Rect(200, 200, 400, 230))
        if role == "Group":
            parent = added
    tree = t.build()
    started = time.perf_counter()
    pruned = prune(tree, goal="open element 4000")
    elapsed = time.perf_counter() - started
    assert len(pruned.candidates) == MAX_CANDIDATES
    assert elapsed < 0.2, f"{elapsed * 1000:.0f} ms"  # the whole observation has 400 ms


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #


@pytest.fixture
def form() -> Iterator[FormWindow]:
    ensure_dpi_awareness()
    work = query_layout().primary.work_area
    with FormWindow(Rect(work.left + 160, work.top + 160, work.left + 660, work.top + 460)) as w:
        yield w


def test_a_live_window_prunes_to_its_controls(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    pruned = prune(tree, goal="save changes")
    by_name = {c.element.name: c for c in pruned.candidates}
    assert pruned.candidates[0].id == 0
    assert pruned.candidates[0].element.name == FormWindow.TITLE
    assert by_name[FormWindow.BUTTON].bbox == form.child_rect("button")
    assert not by_name[FormWindow.DISABLED_BUTTON].element.enabled
    assert by_name[FormWindow.BUTTON].score > by_name[FormWindow.DISABLED_BUTTON].score
    edits = [c for c in pruned.candidates if c.element.role == "Edit"]
    assert len(edits) == 2
    assert [c.element.is_password for c in edits] == [False, True]
    assert edits[1].element.value is None
    assert FormWindow.SECRET not in repr(pruned)
    assert [c.id for c in pruned.candidates] == sorted(c.id for c in pruned.candidates)
