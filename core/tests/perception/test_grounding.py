"""Tests for `perception/grounding.py` — an element id turned into a verified click point.

Two layers:

* **Refusals and choices**, with the fresh walk, both hit tests and the window
  checks replaced, so every way the screen can have moved on since the
  screenshot is one hand-built tree away: a window closed, minimised or on
  another desktop; an element gone, duplicated, renamed, disabled, scrolled
  away or moved; something drawn over it; the monitors changed.
* **Live**, on a real window this test opens: a button grounded onto its own
  pixels, then the same window moved, covered by another window, covered by a
  control of its own, and closed.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Collection, Iterator
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows UI Automation")

from aegis_core.perception import grounding  # noqa: E402
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
    Grounding,
    GroundingError,
    Hit,
    resolve,
)
from aegis_core.perception.prune import PrunedTree, prune  # noqa: E402
from aegis_core.perception.redact import REDACTED, redact_tree  # noqa: E402
from aegis_core.perception.uia_tree import UiaElement, UiaTree, walk  # noqa: E402

from tests.perception.helpers import FormWindow, SolidWindow  # noqa: E402

HWND = 7
WINDOW = Rect(100, 100, 1100, 900)


def _monitor(left: int, width: int, *, primary: bool) -> Monitor:
    bounds = Rect(left, 0, left + width, 1080)
    return Monitor(device=f"D{left}", bounds=bounds, work_area=bounds, dpi=96, primary=primary)


DESK = DisplayLayout(monitors=(_monitor(0, 1920, primary=True),), virtual=Rect(0, 0, 1920, 1080))
WIDER = DisplayLayout(
    monitors=(_monitor(0, 1920, primary=True), _monitor(1920, 1920, primary=False)),
    virtual=Rect(0, 0, 3840, 1080),
)


class Tree:
    """Builds a `UiaTree` element by element; each element's runtime id is `(42, index)`."""

    def __init__(self) -> None:
        self.elements: list[UiaElement] = []
        self.root = self.add(None, "Window", "App", WINDOW)

    def add(self, parent: int | None, role: str, name: str, bbox: Rect | None) -> int:
        index = len(self.elements)
        self.elements.append(
            UiaElement(
                id=index,
                parent=parent,
                depth=0 if parent is None else self.elements[parent].depth + 1,
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
            )
        )
        return index

    def change(self, index: int, **fields: object) -> None:
        self.elements[index] = replace(self.elements[index], **fields)  # type: ignore[arg-type]

    def build(self, layout: DisplayLayout = DESK) -> UiaTree:
        """The tree as `walk()` returns it: not yet redacted."""
        return UiaTree(
            hwnd=HWND,
            elements=tuple(self.elements),
            truncated=False,
            layout=layout,
            captured_at=0.0,
        )


def standard() -> tuple[Tree, int, int, int]:
    """A window holding a toolbar with a *Save* button and a *Quit* button beside it."""
    t = Tree()
    toolbar = t.add(t.root, "ToolBar", "Tools", Rect(120, 120, 800, 180))
    save = t.add(toolbar, "Button", "Save", Rect(140, 130, 240, 170))
    quit_ = t.add(toolbar, "Button", "Quit", Rect(260, 130, 360, 170))
    return t, toolbar, save, quit_


def observe(t: Tree) -> PrunedTree:
    return prune(redact_tree(t.build())[0])


class Screen:
    """Stands in for everything `resolve()` asks Windows: records what it was asked."""

    def __init__(self, fresh: Tree | UiaTree, chain: tuple[tuple[int, ...], ...]) -> None:
        self.fresh = fresh
        self.chain = chain
        self.root_at: int = HWND
        self.window_exists = True
        self.minimised = False
        self.cloaked = False
        self.walked: list[int] = []
        self.stops: list[frozenset[tuple[int, ...]]] = []
        self.points: list[Point] = []

    def walk(self, hwnd: int) -> UiaTree:
        self.walked.append(hwnd)
        return self.fresh.build() if isinstance(self.fresh, Tree) else self.fresh

    def hit_chain(
        self, point: Point, stop: Collection[tuple[int, ...]]
    ) -> tuple[tuple[int, ...], ...]:
        self.points.append(point)
        self.stops.append(frozenset(stop))
        return self.chain


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Screen]:
    def install(fresh: Tree | UiaTree, *chain: tuple[int, ...]) -> Screen:
        fake = Screen(fresh, chain)
        monkeypatch.setattr(grounding, "walk", fake.walk)
        monkeypatch.setattr(grounding, "hit_chain", fake.hit_chain)
        monkeypatch.setattr(grounding, "verify_layout", lambda layout: layout)
        monkeypatch.setattr(display_win32, "is_window", lambda _h: fake.window_exists)
        monkeypatch.setattr(display_win32, "is_minimised", lambda _h: fake.minimised)
        monkeypatch.setattr(display_win32, "is_cloaked", lambda _h: fake.cloaked)
        monkeypatch.setattr(display_win32, "root_window_at", lambda _x, _y: fake.root_at)
        return fake

    return install


def rid(index: int) -> tuple[int, ...]:
    return (42, index)


# --------------------------------------------------------------------------- #
# The point
# --------------------------------------------------------------------------- #


def test_an_element_hit_exactly_resolves_to_the_centre_of_its_box(
    screen: Callable[..., Screen],
) -> None:
    t, _, save, _ = standard()
    fake = screen(t, rid(save))
    result = resolve(observe(t), save)
    assert result == Grounding(
        element_id=save,
        point=Point(190, 150),
        box=Rect(140, 130, 240, 170),
        hwnd=HWND,
        layout=DESK,
        moved=False,
        hit=Hit.TARGET,
    )
    assert fake.walked == [HWND]
    assert fake.points == [Point(190, 150)]


def test_the_centre_of_an_odd_sized_box_is_a_pixel_inside_it(
    screen: Callable[..., Screen],
) -> None:
    t = Tree()
    one = t.add(t.root, "Button", "One pixel", Rect(500, 500, 501, 501))
    screen(t, rid(one))
    assert resolve(observe(t), one).point == Point(500, 500)


def test_a_hit_on_something_inside_the_element_resolves_to_it(
    screen: Callable[..., Screen],
) -> None:
    """The label inside a button, a cell of a row: the click still reaches the element."""
    t, _, save, _ = standard()
    screen(t, (42, 99), rid(save))
    assert resolve(observe(t), save).hit is Hit.INSIDE


def test_a_coarse_hit_on_an_ancestor_alone_resolves_to_the_container(
    screen: Callable[..., Screen],
) -> None:
    """Measured: a real toolbar answers its own hit test for the buttons on it."""
    t, toolbar, save, _ = standard()
    screen(t, rid(toolbar))
    assert resolve(observe(t), save).hit is Hit.CONTAINER


def test_the_hit_test_stops_at_the_element_or_any_of_its_ancestors(
    screen: Callable[..., Screen],
) -> None:
    t, toolbar, save, _ = standard()
    fake = screen(t, rid(save))
    resolve(observe(t), save)
    assert fake.stops == [frozenset({rid(save), rid(toolbar), rid(t.root)})]


def test_an_element_that_moved_is_clicked_where_it_is_now(
    screen: Callable[..., Screen],
) -> None:
    t, _, save, _ = standard()
    observed = observe(t)
    t.change(save, bbox=Rect(540, 330, 640, 370))
    screen(t, rid(save))
    result = resolve(observed, save)
    assert result.point == Point(590, 350)
    assert result.box == Rect(540, 330, 640, 370)
    assert result.moved


def test_only_the_visible_part_of_an_element_is_aimed_at(
    screen: Callable[..., Screen],
) -> None:
    """Half the button hangs off the right of its window: aim at the half that shows."""
    t = Tree()
    wide = t.add(t.root, "Button", "Across", Rect(1000, 200, 1200, 240))
    screen(t, rid(wide))
    result = resolve(observe(t), wide)
    assert result.box == Rect(1000, 200, 1100, 240)
    assert result.point == Point(1050, 220)


def test_a_redacted_field_can_still_be_grounded(screen: Callable[..., Screen]) -> None:
    """Clicking into a password box is how the human is handed it; its name is redacted
    on both sides, so it still matches."""
    t = Tree()
    key = "sk-" + "a" * 24
    field = t.add(t.root, "Edit", f"Token {key}", Rect(200, 200, 400, 240))
    observed = observe(t)
    assert observed.tree.elements[field].name == f"Token {REDACTED}"
    screen(t, rid(field))
    assert resolve(observed, field).hit is Hit.TARGET


# --------------------------------------------------------------------------- #
# What the model answered
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("answer", [99, -1, 3])
def test_an_id_that_is_not_a_candidate_is_refused_before_anything_is_read(
    answer: int, screen: Callable[..., Screen]
) -> None:
    t, _, save, _ = standard()
    observed = prune(redact_tree(t.build())[0], limit=2)  # the window and one more
    fake = screen(t, rid(save))
    with pytest.raises(GroundingError, match=f"Element {answer} is not one of the marked"):
        resolve(observed, answer)
    assert fake.walked == []


def test_an_id_the_model_was_not_shown_is_refused_even_if_it_is_a_candidate(
    screen: Callable[..., Screen],
) -> None:
    """`Marked.unmarked`: a candidate with no box on the picture is not an answer."""
    t, _, save, quit_ = standard()
    fake = screen(t, rid(save))
    with pytest.raises(GroundingError, match="not one of the marked"):
        resolve(observe(t), save, offered=(quit_,))
    assert fake.walked == []
    assert resolve(observe(t), save, offered=(save, quit_)).element_id == save


@pytest.mark.parametrize("answer", [True, "3", 3.0, None])
def test_an_id_that_is_not_a_whole_number_is_refused(
    answer: object, screen: Callable[..., Screen]
) -> None:
    """The id is model output; `True == 1` must not click element 1."""
    t, _, save, _ = standard()
    fake = screen(t, rid(save))
    with pytest.raises(GroundingError, match="whole number"):
        resolve(observe(t), answer)  # type: ignore[arg-type]
    assert fake.walked == []


# --------------------------------------------------------------------------- #
# Refusals: the window
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("window_exists", "has closed"),
        ("minimised", "is minimised"),
        ("cloaked", "another virtual desktop"),
    ],
)
def test_a_window_that_cannot_be_clicked_into_is_refused_before_it_is_read(
    state: str, message: str, screen: Callable[..., Screen]
) -> None:
    t, _, save, _ = standard()
    fake = screen(t, rid(save))
    setattr(fake, state, not getattr(fake, state))
    with pytest.raises(GroundingError, match=message):
        resolve(observe(t), save)
    assert fake.walked == []


def test_a_changed_display_layout_voids_the_observation(screen: Callable[..., Screen]) -> None:
    """`RECOVERY.md § 3.3`: never reuse a pre-change box."""
    t, _, save, _ = standard()
    observed = observe(t)
    fake = screen(t.build(WIDER), rid(save))
    with pytest.raises(LayoutChangedError):
        resolve(observed, save)
    assert fake.points == []


# --------------------------------------------------------------------------- #
# Refusals: the element
# --------------------------------------------------------------------------- #


def test_an_element_that_has_gone_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    observed = observe(t)
    t.change(save, runtime_id=(42, 1000))
    screen(t, rid(save))
    with pytest.raises(GroundingError, match="no longer on screen"):
        resolve(observed, save)


def test_an_element_with_no_identity_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    t.change(save, runtime_id=())
    fake = screen(t, rid(save))
    with pytest.raises(GroundingError, match="cannot be found again"):
        resolve(observe(t), save)
    assert fake.walked == []


def test_an_identity_two_elements_now_share_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, quit_ = standard()
    observed = observe(t)
    t.change(quit_, runtime_id=rid(save))
    screen(t, rid(save))
    with pytest.raises(GroundingError, match="cannot be told apart"):
        resolve(observed, save)


@pytest.mark.parametrize(("field", "now"), [("name", "Pause"), ("role", "MenuItem")])
def test_an_element_that_changed_since_the_screenshot_is_refused(
    field: str, now: str, screen: Callable[..., Screen]
) -> None:
    """*Play* became *Pause*: the same element, and the opposite click."""
    t = Tree()
    play = t.add(t.root, "Button", "Play", Rect(200, 200, 300, 240))
    observed = observe(t)
    t.change(play, **{field: now})
    screen(t, rid(play))
    with pytest.raises(GroundingError, match="has changed since the screenshot") as caught:
        resolve(observed, play)
    assert "Play" not in str(caught.value)
    assert "Pause" not in str(caught.value)


def test_a_disabled_element_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    observed = observe(t)
    t.change(save, enabled=False)
    screen(t, rid(save))
    with pytest.raises(GroundingError, match="disabled"):
        resolve(observed, save)


def test_an_element_scrolled_out_of_view_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    observed = observe(t)
    t.change(save, offscreen=True)
    screen(t, rid(save))
    with pytest.raises(GroundingError, match="scrolled out of view"):
        resolve(observed, save)


@pytest.mark.parametrize(
    "now",
    [
        None,  # no area any more
        Rect(1200, 200, 1300, 240),  # outside its window
    ],
)
def test_an_element_with_no_visible_part_is_refused(
    now: Rect | None, screen: Callable[..., Screen]
) -> None:
    t, _, save, _ = standard()
    observed = observe(t)
    t.change(save, bbox=now)
    fake = screen(t, rid(save))
    with pytest.raises(GroundingError, match="no longer visible"):
        resolve(observed, save)
    assert fake.points == []


# --------------------------------------------------------------------------- #
# Refusals: what is on top
# --------------------------------------------------------------------------- #


def test_another_window_on_top_is_refused_before_ui_automation_is_asked(
    screen: Callable[..., Screen],
) -> None:
    t, _, save, _ = standard()
    fake = screen(t, rid(save))
    fake.root_at = HWND + 1
    with pytest.raises(GroundingError, match="Another window is covering element"):
        resolve(observe(t), save)
    assert fake.points == []


def test_no_window_at_the_point_is_refused(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    fake = screen(t, rid(save))
    fake.root_at = 0
    with pytest.raises(GroundingError, match="Another window"):
        resolve(observe(t), save)


@pytest.mark.parametrize(
    "chain",
    [
        # A sibling drawn over the element, reached through the container they share.
        ((42, 3), (42, 1)),
        # An unknown element climbing all the way to the window.
        ((42, 99), (42, 98), (42, 0)),
        # The sibling on its own: it is not the element and not above it.
        ((42, 3),),
        # An element of another window that Windows' own hit test missed.
        ((7, 1), (7, 0)),
        # Nothing at all.
        (),
    ],
)
def test_anything_else_at_the_point_is_something_drawn_over_the_element(
    chain: tuple[tuple[int, ...], ...], screen: Callable[..., Screen]
) -> None:
    t, _, save, _ = standard()
    screen(t, *chain)
    with pytest.raises(GroundingError, match="Something else is drawn over element 2"):
        resolve(observe(t), save)


def test_every_refusal_says_what_to_do_next(screen: Callable[..., Screen]) -> None:
    t, _, save, _ = standard()
    fake = screen(t, (42, 3))
    with pytest.raises(GroundingError) as caught:
        resolve(observe(t), save)
    assert str(caught.value).endswith(grounding.OBSERVE_AGAIN)
    fake.minimised = True
    with pytest.raises(GroundingError) as caught:
        resolve(observe(t), save)
    assert str(caught.value).endswith(grounding.OBSERVE_AGAIN)


def test_no_screen_text_reaches_the_log(
    screen: Callable[..., Screen], caplog: pytest.LogCaptureFixture
) -> None:
    t = Tree()
    secretive = t.add(t.root, "Button", "Wire 4000 to account 12345678", Rect(200, 200, 400, 240))
    screen(t, rid(secretive))
    with caplog.at_level(logging.DEBUG, logger="aegis_core.perception.grounding"):
        resolve(observe(t), secretive)
    assert caplog.records
    for record in caplog.records:
        assert "12345678" not in repr(record.__dict__)


# --------------------------------------------------------------------------- #
# Live: a real window through the real UI Automation stack
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module", autouse=True)
def _per_monitor_aware() -> None:
    ensure_dpi_awareness()


def _form_rect(offset: int = 0) -> Rect:
    work = query_layout().primary.work_area
    left, top = work.left + 160 + offset, work.top + 160 + offset
    return Rect(left, top, left + 500, top + 300)


@pytest.fixture
def form() -> Iterator[FormWindow]:
    with FormWindow(_form_rect()) as window:
        yield window


def _observe(form: FormWindow) -> PrunedTree:
    return prune(redact_tree(walk(form.hwnd))[0])


def _id_of(pruned: PrunedTree, name: str) -> int:
    matches = [c.id for c in pruned.candidates if c.element.name == name]
    assert len(matches) == 1
    return matches[0]


def test_a_live_button_is_grounded_on_its_own_pixels(form: FormWindow) -> None:
    observed = _observe(form)
    result = resolve(observed, _id_of(observed, FormWindow.BUTTON))
    button = form.child_rect("button")
    assert result.hit is Hit.TARGET
    assert button.contains(result.point)
    assert result.point == Point(button.left + button.width // 2, button.top + button.height // 2)
    assert not result.moved
    # Where Windows itself would deliver a click there.
    assert display_win32.root_window_at(result.point.x, result.point.y) == form.hwnd


def test_a_live_password_box_can_be_grounded_without_its_contents(form: FormWindow) -> None:
    observed = _observe(form)
    password = next(c for c in observed.candidates if c.element.is_password)
    result = resolve(observed, password.id)
    assert form.child_rect("password").contains(result.point)


def test_a_live_disabled_button_is_refused(form: FormWindow) -> None:
    observed = _observe(form)
    with pytest.raises(GroundingError, match="disabled"):
        resolve(observed, _id_of(observed, FormWindow.DISABLED_BUTTON))


def test_a_live_window_that_moved_is_clicked_where_it_is_now(form: FormWindow) -> None:
    observed = _observe(form)
    element_id = _id_of(observed, FormWindow.BUTTON)
    moved = _form_rect(offset=120)
    form.move_to(moved.left, moved.top)
    result = resolve(observed, element_id)
    assert result.moved
    assert form.child_rect("button").contains(result.point)


def test_a_live_window_covered_by_another_is_refused(form: FormWindow) -> None:
    observed = _observe(form)
    button = form.child_rect("button")
    cover = SolidWindow(
        Rect(button.left - 10, button.top - 10, button.right + 10, button.bottom + 10)
    )
    try:
        with pytest.raises(GroundingError, match="Another window is covering"):
            resolve(observed, _id_of(observed, FormWindow.BUTTON))
    finally:
        cover.destroy()


def test_a_live_control_covered_inside_its_own_window_is_refused(form: FormWindow) -> None:
    """Only UI Automation's hit test can see this one: no other window is involved."""
    observed = _observe(form)
    form.cover("button")
    with pytest.raises(GroundingError, match="Something else is drawn over"):
        resolve(observed, _id_of(observed, FormWindow.BUTTON))


def test_a_live_label_a_click_passes_through_does_not_cover_anything(form: FormWindow) -> None:
    """A static label without `SS_NOTIFY` is transparent to a real click, and both hit
    tests agree: the button beneath is what a click there reaches."""
    observed = _observe(form)
    form.cover("button", takes_clicks=False)
    result = resolve(observed, _id_of(observed, FormWindow.BUTTON))
    assert form.child_rect("button").contains(result.point)


def test_a_live_window_that_closed_is_refused() -> None:
    with FormWindow(_form_rect()) as window:
        observed = _observe(window)
        element_id = _id_of(observed, FormWindow.BUTTON)
    with pytest.raises(GroundingError, match="has closed"):
        resolve(observed, element_id)
