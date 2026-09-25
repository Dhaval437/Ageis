"""Absolute gestures and named keys (`P3-01`).

Three layers: `parse_combo` against a fake keyboard layout and the real one; the
controller's `move_to` / `click_at` / `drag_to` / `press_keys` against a recording
backend and a synthetic mixed-DPI desk; and the real cursor, moved by the real
controller and read back with `GetCursorPos`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest
from aegis_core.actuation import keys
from aegis_core.actuation.input import InputAbortedError, InputController, MouseButton
from aegis_core.actuation.keys import (
    KEY_NAMES,
    MODIFIER_NAMES,
    VK_CONTROL,
    VK_F1,
    VK_LEFT,
    VK_LWIN,
    VK_MENU,
    VK_SHIFT,
    KeyNameError,
    parse_combo,
)
from aegis_core.actuation.preempt import PreemptEvent, PreemptSignal
from aegis_core.perception.display import (
    DisplayLayout,
    LayoutChangedError,
    OffScreenError,
    Point,
)

from tests.actuation.helpers import RecordingBackend
from tests.perception.test_display import MIXED, PRIMARY

VK_OEM_2 = 0xBF  # `/?` on a US layout


def us_layout(char: str) -> tuple[int, int] | None:
    """A fake US layout for `VkKeyScanW`: enough to test the shift-state logic."""
    table = {"/": (VK_OEM_2, 0), "?": (VK_OEM_2, 1), "=": (0xBB, 0), "+": (0xBB, 1)}
    return table.get(char)


# --------------------------------------------------------------------------- #
# parse_combo
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("ctrl+s", (VK_CONTROL, ord("S"))),
        ("Ctrl + Shift + N", (VK_CONTROL, VK_SHIFT, ord("N"))),
        ("alt+f4", (VK_MENU, VK_F1 + 3)),
        ("win+left", (VK_LWIN, VK_LEFT)),
        ("enter", (0x0D,)),
        ("return", (0x0D,)),
        ("esc", (0x1B,)),
        ("f24", (0x87,)),
        ("numpad7", (0x67,)),
        ("ctrl+0", (VK_CONTROL, 0x30)),
        ("pgdn", (0x22,)),
        ("altgr+e", (0xA5, ord("E"))),
    ],
)
def test_named_keys(spec: str, expected: tuple[int, ...]) -> None:
    assert parse_combo(spec, lookup=us_layout) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("ctrl+/", (VK_CONTROL, VK_OEM_2)),
        ("ctrl+?", (VK_CONTROL, VK_SHIFT, VK_OEM_2)),
        ("shift+?", (VK_SHIFT, VK_OEM_2)),
        ("ctrl++", (VK_CONTROL, VK_SHIFT, 0xBB)),
        ("+", (VK_SHIFT, 0xBB)),
        ("ctrl+=", (VK_CONTROL, 0xBB)),
    ],
)
def test_characters_come_from_the_keyboard_layout(spec: str, expected: tuple[int, ...]) -> None:
    assert parse_combo(spec, lookup=us_layout) == expected


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ("", "1 to 64"),
        ("x" * 65, "1 to 64"),
        ("ctrl+", "empty part"),
        ("ctrl++shift", "empty part"),  # `+` is a key only at the end
        ("ctrl+a+b", "one key besides"),
        ("ctrl+shift", "needs a key"),
        ("ctrl+ctrl+a", "named twice"),
        ("hyper+a", "not a key"),
        ("ctrl+§", "has no key"),
    ],
)
def test_what_cannot_be_read_is_refused(spec: str, fragment: str) -> None:
    with pytest.raises(KeyNameError, match=fragment):
        parse_combo(spec, lookup=us_layout)


def test_a_refusal_quotes_at_most_a_short_name() -> None:
    with pytest.raises(KeyNameError) as error:
        parse_combo("ctrl+" + "z" * 50, lookup=us_layout)
    assert "z" * 25 not in str(error.value)


def test_names_are_unambiguous() -> None:
    assert not KEY_NAMES & MODIFIER_NAMES
    assert all(name == name.lower() and "+" not in name for name in KEY_NAMES | MODIFIER_NAMES)


@pytest.mark.skipif(sys.platform != "win32", reason="the real keyboard layout")
def test_a_character_on_the_real_layout() -> None:
    from aegis_core.actuation import win32

    found = win32.vk_for_char("/")
    assert found is not None
    assert parse_combo("ctrl+/")[-1] == found[0]


# --------------------------------------------------------------------------- #
# The controller, against a synthetic desk
# --------------------------------------------------------------------------- #


def controller(
    backend: RecordingBackend,
    *,
    check: Callable[[DisplayLayout], DisplayLayout] | None = None,
    signal: PreemptSignal | None = None,
) -> InputController:
    return InputController(
        backend, signal, inter_event_delay=0, layout_check=check or (lambda layout: layout)
    )


def moves(backend: RecordingBackend) -> list[object]:
    return [e.detail for e in backend.events if e.kind == "move_to"]


def test_move_to_sends_one_absolute_move(recording: RecordingBackend) -> None:
    point = Point(-100, 700)  # on the left monitor, left of the virtual origin
    controller(recording).move_to(point, MIXED)
    assert moves(recording) == [MIXED.to_absolute(point)]


def test_move_to_re_reads_the_layout_and_uses_the_fresh_one(recording: RecordingBackend) -> None:
    seen: list[DisplayLayout] = []

    def check(layout: DisplayLayout) -> DisplayLayout:
        seen.append(layout)
        return layout

    controller(recording, check=check).move_to(Point(10, 10), MIXED)
    assert seen == [MIXED]


def test_a_changed_layout_moves_nothing(recording: RecordingBackend) -> None:
    def changed(_layout: DisplayLayout) -> DisplayLayout:
        raise LayoutChangedError("a monitor was unplugged")

    with pytest.raises(LayoutChangedError):
        controller(recording, check=changed).click_at(Point(10, 10), MIXED)
    assert recording.events == []


@pytest.mark.parametrize("point", [Point(-10, -10), Point(3000, 1000), Point(-1, 100)])
def test_a_point_on_no_monitor_moves_nothing(recording: RecordingBackend, point: Point) -> None:
    with pytest.raises(OffScreenError):
        controller(recording).move_to(point, MIXED)
    assert recording.events == []


@pytest.mark.parametrize(
    ("button", "count"),
    [(MouseButton.LEFT, 1), (MouseButton.LEFT, 2), (MouseButton.RIGHT, 1)],
    ids=["click", "double", "right"],
)
def test_click_at_moves_then_clicks(
    recording: RecordingBackend, button: MouseButton, count: int
) -> None:
    controller(recording).click_at(Point(500, 500), MIXED, button=button, count=count)
    assert recording.kinds() == ["move_to"] + ["button", "button"] * count
    assert [e.detail for e in recording.events if e.kind == "button"] == [button] * 2 * count


def test_drag_to_presses_moves_and_releases(recording: RecordingBackend) -> None:
    ctrl = controller(recording)
    ctrl.drag_to(Point(100, 100), Point(900, 500), MIXED, steps=4)
    assert recording.kinds() == ["move_to", "button", *["move_to"] * 4, "button"]
    assert moves(recording)[0] == MIXED.to_absolute(Point(100, 100))
    assert moves(recording)[-1] == MIXED.to_absolute(Point(900, 500))
    assert ctrl.held_buttons == ()


def test_a_drag_across_a_dead_zone_never_presses(recording: RecordingBackend) -> None:
    # From the left monitor to the top one: the straight line crosses a corner no
    # monitor shows, so the drag is refused before the button goes down.
    with pytest.raises(OffScreenError):
        controller(recording).drag_to(Point(-1800, 300), Point(1000, -1000), MIXED, steps=10)
    assert recording.events == []


def test_an_abort_mid_drag_releases_the_button() -> None:
    signal = PreemptSignal()

    class Interrupting(RecordingBackend):
        def mouse_move_absolute(self, ax: int, ay: int) -> None:
            super().mouse_move_absolute(ax, ay)
            if len(moves(self)) == 3:
                signal.trigger(PreemptEvent("mouse", 0x0200, 0.0))

    backend = Interrupting()
    ctrl = controller(backend, signal=signal)
    with pytest.raises(InputAbortedError) as error:
        ctrl.drag_to(Point(100, 100), Point(900, 500), MIXED, steps=8)
    assert error.value.released_buttons == (MouseButton.LEFT,)
    assert ctrl.held_buttons == ()
    assert len(moves(backend)) == 3


def test_press_keys_holds_in_order_and_releases_in_reverse(recording: RecordingBackend) -> None:
    ctrl = controller(recording)
    ctrl.press_keys("ctrl+shift+n")
    assert recording.key_downs() == [VK_CONTROL, VK_SHIFT, ord("N")]
    assert recording.key_ups() == [ord("N"), VK_SHIFT, VK_CONTROL]
    assert ctrl.held_keys == ()


def test_press_keys_refuses_before_sending_anything(recording: RecordingBackend) -> None:
    with pytest.raises(KeyNameError):
        controller(recording).press_keys("ctrl+nonsense")
    assert recording.events == []


@pytest.fixture
def recording() -> RecordingBackend:
    return RecordingBackend()


def test_the_primary_fixture_is_on_screen() -> None:
    # Guard for the tests above: they assume these points are real.
    assert MIXED.monitor_at(Point(500, 500)) == PRIMARY
    assert keys.is_extended(VK_LEFT, 0x4B)


# --------------------------------------------------------------------------- #
# Live: the real controller puts the real cursor on exactly the pixel asked for
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform != "win32", reason="the real desktop")
def test_move_to_lands_on_every_probe_point_of_the_live_desk() -> None:
    """`move_to` through `SendInputBackend`, read back with `GetCursorPos`.

    The same probe as `test_display.py`'s mapping test, but through the code the
    agent will actually call, including the layout re-check. A point is retried
    once, because a person nudging the mouse is indistinguishable from a wrong
    mapping on one sample; a wrong mapping misses every time.
    """
    from aegis_core.actuation.input import SendInputBackend
    from aegis_core.perception.display import ensure_dpi_awareness, query_layout

    from tests.perception.test_display import _cursor, _probe_points

    ensure_dpi_awareness()
    start = _cursor()
    if start is None:
        pytest.skip("no interactive desktop: GetCursorPos is refused")
    layout = query_layout()
    ctrl = InputController(SendInputBackend(), inter_event_delay=0)
    misses: list[tuple[Point, Point | None]] = []
    try:
        for point in _probe_points(layout)[::7]:
            for _attempt in range(2):
                ctrl.move_to(point, layout)
                landed = _cursor()
                if landed == point:
                    break
            else:
                misses.append((point, landed))
    finally:
        ctrl.move_to(start, query_layout())
    assert not misses, f"{len(misses)} points missed, e.g. {misses[:5]}"
