"""Tests for `pace.py`, the helper the perception timing gates stand on (`P2-15`).

A gate that cannot fail is worse than none, so the helper is held to what it claims
with a fake clock: it reports the work in units of the calibration, pair by pair,
and load that slows both alike changes nothing.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable

import pytest

from tests.perception import pace as pace_module
from tests.perception.pace import (
    ROUNDS,
    assert_keeps_pace,
    calibrate,
    calibration_text,
    pace,
    wall_clock_ms,
)


class FakeMachine:
    """A clock that only moves when work is done, at a speed the test sets."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, calibration_s: float = 10.0) -> None:
        self.now = 0.0
        self.slowdown = 1.0
        self.work_calls = 0
        self.calibration_calls = 0
        #: Called at the start of each calibration, with how many came before it.
        self.before_calibration: Callable[[int], None] = lambda _count: None
        self._calibration_s = calibration_s
        monkeypatch.setattr("tests.perception.pace.perf_counter", lambda: self.now)
        monkeypatch.setattr(pace_module, "calibrate", self._calibrate)

    def _calibrate(self) -> None:
        self.before_calibration(self.calibration_calls)
        self.calibration_calls += 1
        self.now += self._calibration_s * self.slowdown

    def work(self, seconds: float) -> Callable[[], None]:
        def run() -> None:
            self.work_calls += 1
            self.now += seconds * self.slowdown

        return run


def test_the_work_is_reported_in_units_of_the_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(monkeypatch)
    ratios = pace(machine.work(30.0))
    assert ratios == [3.0] * ROUNDS


def test_each_is_run_once_untimed_and_then_once_a_round(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(monkeypatch)
    assert len(pace(machine.work(30.0), rounds=4)) == 4
    assert machine.work_calls == 5
    assert machine.calibration_calls == 5


def test_a_machine_that_slows_down_part_way_changes_no_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why each pair's ratio is taken before the median: three quiet pairs and four
    at a third of the speed are the same seven ratios."""
    machine = FakeMachine(monkeypatch)

    def load_arrives(calibrations_so_far: int) -> None:
        if calibrations_so_far == 4:
            machine.slowdown = 3.0

    machine.before_calibration = load_arrives
    assert pace(machine.work(30.0)) == [3.0] * ROUNDS
    assert machine.slowdown == 3.0


def test_a_pair_split_by_a_change_in_load_is_an_outlier_the_median_ignores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = FakeMachine(monkeypatch)
    calls = 0

    def work() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:  # between a calibration and its work
            machine.slowdown = 3.0
        machine.now += 30.0 * machine.slowdown

    ratios = pace(work)
    assert ratios.count(9.0) == 1
    assert statistics.median(ratios) == 3.0


def test_work_inside_the_ceiling_passes_and_work_past_it_fails_showing_every_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = FakeMachine(monkeypatch)
    assert_keeps_pace(machine.work(30.0), 3.2)
    with pytest.raises(AssertionError) as failure:
        assert_keeps_pace(machine.work(33.0), 3.2)
    message = str(failure.value)
    assert "3.30 x the calibration" in message
    assert "ceiling 3.2" in message
    assert message.count("3.3") >= ROUNDS


def test_the_ceiling_itself_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(monkeypatch)
    with pytest.raises(AssertionError):
        assert_keeps_pace(machine.work(30.0), 3.0)


def test_a_loaded_machine_passes_what_a_quiet_one_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole module: ten times slower across the board is not a
    regression, and the same work fails a wall-clock ceiling it met when quiet."""
    machine = FakeMachine(monkeypatch)
    work = machine.work(30.0)
    quiet_ms = wall_clock_ms(work)
    machine.slowdown = 10.0
    assert_keeps_pace(work, 3.2)
    assert wall_clock_ms(work) == pytest.approx(quiet_ms * 10)


def test_the_wall_clock_reading_is_the_median_after_one_untimed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = FakeMachine(monkeypatch)
    durations = iter([9.0, 0.020, 0.021, 0.500, 0.019, 0.020, 0.022, 0.018])
    machine.now = 0.0

    def work() -> None:
        machine.now += next(durations)

    assert wall_clock_ms(work) == pytest.approx(20.0)


def test_the_calibration_is_the_same_megabyte_every_time() -> None:
    text = calibration_text()
    assert len(text) == 1_000_000
    assert calibration_text() is text
    assert pace_module.calibration_text.__wrapped__() == text


def test_the_real_calibration_costs_about_one_of_itself() -> None:
    """The one test here on the real clock: the workload measured against itself is
    ~1 whatever the machine is doing, which is the property the gates rely on."""
    assert 0.5 < statistics.median(pace(calibrate)) < 2.0
