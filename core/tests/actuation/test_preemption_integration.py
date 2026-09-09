"""Timed end-to-end preemption (`P3-05`), against real hooks.

The promise in `REMEMBER.md` invariant 1 and `UI.md § 7` is a wall-clock one:
from the moment the human touches the keyboard, the agent has 100 ms to have
stopped. This measures the whole path — Windows' input queue, the hook thread,
the signal, the action thread noticing, and every held modifier being released —
and fails if it does not fit in the budget.

The action under test runs against a recording backend, so nothing is typed into
the developer's desktop; only the *trigger* is real input.
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Win32 low-level hooks")

from aegis_core.actuation.input import InputAbortedError, InputController  # noqa: E402
from aegis_core.actuation.preempt import InputMonitor, PreemptSignal  # noqa: E402

from tests.actuation.helpers import (  # noqa: E402
    RecordingBackend,
    inject_untagged_key,
    settle,
    wait_until,
)
from tests.actuation.test_abort import BOTH_SIDED_MODIFIERS  # noqa: E402

#: `ARCHITECTURE.md § 8.3` / `UI.md § 7`.
BUDGET_MS = 100.0
RUNS = 5
#: Long enough that the action is unambiguously still in flight when we inject.
LONG_TEXT = "x" * 20_000


@dataclass
class Outcome:
    latency_ms: float
    released: tuple[int, ...]
    still_held: tuple[int, ...]
    typed: int


def run_once(signal: PreemptSignal) -> Outcome:
    """Hold all eight modifiers, start typing, then take over as a human would."""
    backend = RecordingBackend(delay=0.0002)
    controller = InputController(backend, signal, inter_event_delay=0.0005)
    aborted_at: list[float] = []
    error: list[BaseException] = []
    released: list[tuple[int, ...]] = []

    def act() -> None:
        try:
            for vk in BOTH_SIDED_MODIFIERS:
                controller.key_down(vk)
            controller.type_text(LONG_TEXT)
        except InputAbortedError as exc:
            # Stamped only after release_all() has finished, so the measurement
            # covers the modifier release too — not just noticing the signal.
            aborted_at.append(time.perf_counter())
            released.append(exc.released)
        except BaseException as exc:  # a release failure must fail the test loudly
            aborted_at.append(time.perf_counter())
            error.append(exc)

    thread = threading.Thread(target=act, name="aegis-action")
    thread.start()
    try:
        assert wait_until(lambda: len(controller.held_keys) == len(BOTH_SIDED_MODIFIERS)), (
            "the action never got all eight modifiers down"
        )
        assert wait_until(lambda: backend.kinds().count("char") > 5), "typing never started"

        sent_at = time.perf_counter()
        inject_untagged_key()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the action never aborted"
    finally:
        controller.release_all()

    if error:
        raise error[0]
    assert aborted_at, "the action finished without aborting"
    return Outcome(
        latency_ms=(aborted_at[0] - sent_at) * 1000,
        released=released[0],
        still_held=controller.held_keys,
        typed=backend.kinds().count("char"),
    )


@pytest.fixture
def signal() -> Iterator[PreemptSignal]:
    """Keyboard-only, deliberately.

    The trigger has to be *our* injected keystroke for the measurement to mean
    anything. A monitor that also watched the mouse would happily be set off by
    the developer's cursor a moment before the injection, and the number this
    test prints would be nonsense. `test_hooks.py` covers the mouse path.
    """
    signal = PreemptSignal()
    with InputMonitor(signal, watch_mouse=False):
        yield signal


def test_physical_input_stops_the_agent_within_100ms(signal: PreemptSignal) -> None:
    outcomes: list[Outcome] = []
    for _ in range(RUNS):
        assert settle(signal), "someone was typing during the run"
        outcomes.append(run_once(signal))

    latencies = [outcome.latency_ms for outcome in outcomes]
    worst = max(latencies)
    assert worst < BUDGET_MS, (
        f"preemption took {worst:.1f} ms (budget {BUDGET_MS:.0f} ms); all runs: "
        + ", ".join(f"{value:.1f}" for value in latencies)
    )
    # Reported so a regression that merely creeps toward the budget is visible.
    assert statistics.median(latencies) < BUDGET_MS


def test_the_in_flight_action_aborts_mid_sequence(signal: PreemptSignal) -> None:
    assert settle(signal), "someone was typing during the run"
    outcome = run_once(signal)
    assert 0 < outcome.typed < len(LONG_TEXT), "the action did not stop part-way"


def test_no_modifier_survives_a_real_preemption(signal: PreemptSignal) -> None:
    """The P0 case, end to end: real input in, eight KEYUPs out, nothing held."""
    assert settle(signal), "someone was typing during the run"
    outcome = run_once(signal)

    assert set(outcome.released) == set(BOTH_SIDED_MODIFIERS)
    assert outcome.still_held == ()
