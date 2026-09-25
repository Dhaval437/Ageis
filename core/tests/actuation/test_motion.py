"""Human-like pointer motion (`P3-03`): the planner, the controller, the real cursor."""

from __future__ import annotations

import dataclasses
import itertools
import math
import random
import sys
import threading
import time
from typing import cast

import pytest
from aegis_core.actuation.input import InputAbortedError, InputController, MouseButton
from aegis_core.actuation.motion import (
    HUMAN,
    MIN_TRAVEL_PX,
    Motion,
    duration,
    plan_path,
)
from aegis_core.actuation.preempt import PreemptEvent, PreemptSignal
from aegis_core.perception.display import OffScreenError, Point

from tests.actuation.helpers import RecordingBackend
from tests.perception.test_display import MIXED


def seeded(seed: int) -> random.Random:
    """A fixed generator: these tests are about geometry, not randomness."""
    return random.Random(seed)  # noqa: S311 - motion, not secrets


#: Where the fake cursor starts in the controller tests.
CURSOR = Point(100, 100)

#: A motion with the same geometry as HUMAN but no waiting, for fast tests.
QUICK = Motion(min_duration=0.001, max_duration=0.002, step_interval=0.0001, dwell=(0, 0))


def distance_from_line(p: Point, a: Point, b: Point) -> float:
    return abs((b.x - a.x) * (a.y - p.y) - (a.x - p.x) * (b.y - a.y)) / math.dist(
        (a.x, a.y), (b.x, b.y)
    )


# --------------------------------------------------------------------------- #
# The planner
# --------------------------------------------------------------------------- #

CASES = [
    (Point(0, 0), Point(1000, 400)),
    (Point(900, 900), Point(10, 20)),
    (Point(-1500, 600), Point(-1400, 610)),
    (Point(50, 50), Point(50, 1200)),
]


@pytest.mark.parametrize(("start", "end"), CASES)
@pytest.mark.parametrize("seed", range(20))
def test_every_path_ends_exactly_on_the_target(start: Point, end: Point, seed: int) -> None:
    path = plan_path(start, end, HUMAN, seeded(seed))
    assert path[-1] == end
    assert all(a != b for a, b in itertools.pairwise(path))


@pytest.mark.parametrize(("start", "end"), CASES)
@pytest.mark.parametrize("seed", range(20))
def test_a_path_bows_but_stays_near_the_line(start: Point, end: Point, seed: int) -> None:
    path = plan_path(start, end, HUMAN, seeded(seed))
    bound = HUMAN.curvature * math.dist((start.x, start.y), (end.x, end.y)) + HUMAN.jitter_px + 1
    assert max(distance_from_line(p, start, end) for p in path) <= bound


def test_a_path_is_curved_not_ruled() -> None:
    start, end = Point(0, 0), Point(1000, 0)
    bows = [max(abs(p.y) for p in plan_path(start, end, HUMAN, seeded(seed))) for seed in range(20)]
    assert max(bows) > 20  # most seeds bow visibly; a straight line would be 0-1


def test_a_straight_motion_is_straight() -> None:
    path = plan_path(Point(0, 0), Point(1000, 0), HUMAN.straight(), seeded(1))
    assert all(p.y == 0 for p in path)


def test_it_eases_in_and_out() -> None:
    path = plan_path(Point(0, 0), Point(1000, 0), HUMAN.straight(), seeded(1))
    steps = [b.x - a.x for a, b in itertools.pairwise([Point(0, 0), *path])]
    middle = steps[len(steps) // 2]
    # Smoothstep makes the first and last steps a small fraction of the middle ones.
    assert steps[0] * 4 < middle and steps[-1] * 4 < middle


def test_it_is_deterministic_for_a_seed() -> None:
    a = plan_path(Point(0, 0), Point(800, 300), HUMAN, seeded(7))
    b = plan_path(Point(0, 0), Point(800, 300), HUMAN, seeded(7))
    assert a == b


@pytest.mark.parametrize("gap", [0, 1, MIN_TRAVEL_PX - 1])
def test_a_tiny_move_is_just_the_target(gap: int) -> None:
    assert plan_path(Point(10, 10), Point(10 + gap, 10), HUMAN, seeded(1)) == [Point(10 + gap, 10)]


def test_the_step_count_is_bounded() -> None:
    # A fine step interval asks for ~450 points; the cap is what stops it.
    fine = dataclasses.replace(HUMAN, step_interval=0.001)
    far = plan_path(Point(0, 0), Point(30_000, 30_000), fine, seeded(1))
    assert len(far) <= fine.max_steps
    near = plan_path(Point(0, 0), Point(40, 0), HUMAN, seeded(1))
    assert len(near) >= 2


def test_durations_follow_fitts_law_within_bounds() -> None:
    assert duration(0, HUMAN) == HUMAN.min_duration
    assert duration(100_000, HUMAN) == HUMAN.max_duration
    assert duration(100, HUMAN) < duration(1000, HUMAN) < duration(3000, HUMAN)


@pytest.mark.parametrize(
    "bad",
    [
        {"min_duration": 0},
        {"min_duration": 0.5, "max_duration": 0.1},
        {"step_interval": 0},
        {"max_steps": 1},
        {"curvature": 0.8},
        {"jitter_px": -1},
        {"dwell": (0.2, 0.1)},
    ],
)
def test_nonsense_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Motion(**bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #


def human(
    backend: RecordingBackend,
    cursor: Point | None = CURSOR,
    *,
    motion: Motion = QUICK,
    signal: PreemptSignal | None = None,
) -> InputController:
    return InputController(
        backend,
        signal,
        inter_event_delay=0,
        layout_check=lambda layout: layout,
        motion=motion,
        cursor=lambda: cursor,
        rng=seeded(3),
    )


def moves(backend: RecordingBackend) -> list[tuple[int, int]]:
    return [cast("tuple[int, int]", e.detail) for e in backend.events if e.kind == "move_to"]


def test_move_to_travels_and_lands_on_the_target() -> None:
    backend = RecordingBackend()
    human(backend).move_to(Point(1500, 900), MIXED)
    assert len(moves(backend)) > 5
    assert moves(backend)[-1] == MIXED.to_absolute(Point(1500, 900))


def test_with_no_known_cursor_it_jumps() -> None:
    backend = RecordingBackend()
    human(backend, cursor=None).move_to(Point(1500, 900), MIXED)
    assert moves(backend) == [MIXED.to_absolute(Point(1500, 900))]


def test_a_curve_off_every_monitor_falls_back_to_a_straight_line() -> None:
    # Along the top edge of the primary: any bow upward leaves the desk... unless
    # it goes into the monitor above, so use the left monitor's top edge.
    backend = RecordingBackend()
    ctrl = human(
        backend, cursor=Point(-1900, 200), motion=dataclasses.replace(QUICK, curvature=0.5)
    )
    ctrl.move_to(Point(-100, 200), MIXED)
    travelled = moves(backend)
    # It still travelled — a jump would be one move — and along the edge, not above it.
    assert len(travelled) > 5
    assert {ay for _ax, ay in travelled} == {MIXED.to_absolute(Point(-100, 200))[1]}
    assert travelled[-1] == MIXED.to_absolute(Point(-100, 200))


def test_a_move_whose_line_crosses_a_dead_zone_jumps() -> None:
    backend = RecordingBackend()
    human(backend, cursor=Point(-1800, 300)).move_to(Point(1000, -1000), MIXED)
    assert moves(backend) == [MIXED.to_absolute(Point(1000, -1000))]


def test_a_drag_across_a_dead_zone_still_never_starts() -> None:
    backend = RecordingBackend()
    with pytest.raises(OffScreenError):
        human(backend).drag_to(Point(-1800, 300), Point(1000, -1000), MIXED)
    assert backend.events == []


def test_click_at_rests_before_pressing() -> None:
    backend = RecordingBackend()
    slow_dwell = dataclasses.replace(QUICK, dwell=(0.05, 0.05))
    ctrl = human(backend, motion=slow_dwell)
    started = time.perf_counter()
    ctrl.click_at(Point(700, 700), MIXED)
    assert time.perf_counter() - started >= 0.05
    kinds = backend.kinds()
    assert kinds[-2:] == ["button", "button"] and set(kinds[:-2]) == {"move_to"}


def test_a_human_drag_presses_travels_and_releases() -> None:
    backend = RecordingBackend()
    ctrl = human(backend)
    ctrl.drag_to(Point(300, 300), Point(1200, 800), MIXED)
    kinds = backend.kinds()
    press, release = kinds.index("button"), len(kinds) - 1
    assert kinds[release] == "button"
    assert kinds[press + 1 : release].count("move_to") > 5
    assert moves(backend)[-1] == MIXED.to_absolute(Point(1200, 800))
    assert ctrl.held_buttons == ()


def test_preemption_mid_travel_stops_it_and_mid_drag_releases_the_button() -> None:
    signal = PreemptSignal()

    class Interrupting(RecordingBackend):
        """Preempts on the third move after the button went down: mid-drag."""

        def mouse_move_absolute(self, ax: int, ay: int) -> None:
            super().mouse_move_absolute(ax, ay)
            kinds = self.kinds()
            if "button" in kinds and kinds[kinds.index("button") :].count("move_to") == 3:
                signal.trigger(PreemptEvent("mouse", 0x0200, 0.0))

    backend = Interrupting()
    ctrl = human(backend, signal=signal)
    with pytest.raises(InputAbortedError) as error:
        ctrl.drag_to(Point(300, 300), Point(1200, 800), MIXED)
    kinds = backend.kinds()
    assert kinds[kinds.index("button") :].count("move_to") == 3  # stopped at once
    assert error.value.released_buttons == (MouseButton.LEFT,)
    assert ctrl.held_buttons == ()


# --------------------------------------------------------------------------- #
# Live: the real cursor travels and lands, at a human pace
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform != "win32", reason="the real desktop")
def test_a_human_move_on_the_live_desk_lands_exactly_and_in_time() -> None:
    from aegis_core.actuation.input import SendInputBackend
    from aegis_core.perception.display import ensure_dpi_awareness, query_layout

    from tests.perception.test_display import _cursor

    ensure_dpi_awareness()
    start = _cursor()
    if start is None:
        pytest.skip("no interactive desktop: GetCursorPos is refused")
    layout = query_layout()
    primary = layout.primary.bounds
    targets = [
        Point(primary.left + primary.width // 4, primary.top + primary.height // 4),
        Point(primary.right - primary.width // 4, primary.bottom - primary.height // 3),
    ]
    ctrl = InputController(SendInputBackend(), inter_event_delay=0, motion=HUMAN)
    try:
        for target in targets:
            for _attempt in range(2):  # a person nudging the mouse is not a bug
                began = time.perf_counter()
                ctrl.move_to(target, layout)
                took = time.perf_counter() - began
                if _cursor() == target:
                    break
            assert _cursor() == target
            assert HUMAN.min_duration * 0.5 <= took <= HUMAN.max_duration * 3
    finally:
        InputController(SendInputBackend(), inter_event_delay=0).move_to(start, query_layout())


def test_preemption_ends_a_pause_at_once() -> None:
    """A slow move is still stopped the moment the human touches the mouse: the
    pause between steps waits on the preemption signal, not on a clock."""

    signal = PreemptSignal()
    slow = dataclasses.replace(QUICK, min_duration=5.0, max_duration=5.0, step_interval=2.5)
    ctrl = human(RecordingBackend(), motion=slow, signal=signal)
    threading.Timer(0.1, signal.trigger, args=(PreemptEvent("mouse", 0x0200, 0.0),)).start()
    started = time.perf_counter()
    with pytest.raises(InputAbortedError):
        ctrl.move_to(Point(1500, 900), MIXED)
    assert time.perf_counter() - started < 1.0
