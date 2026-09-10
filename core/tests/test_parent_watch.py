"""The core stops when its supervisor does (`ARCHITECTURE.md § 3.1` step 6).

Two layers here. The unit tests drive `ParentWatch` through an injected identity
function, so PID reuse and a refused lookup are exercised deterministically. The
last test spawns a real supervisor process and kills it, which is the only way to
prove the thread, the poll and the timing actually work together — that is the
`REMEMBER.md` invariant 14 guarantee, and a mocked version of it proves nothing.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
from aegis_core.parent_watch import GONE, UNIDENTIFIED, Identity, ParentWatch, process_identity

#: § 3.1 step 6's budget. The watch must fire well inside it.
STEP_6_BUDGET_S = 2.0


class Recorder:
    """Counts how many times the watch reported the supervisor gone."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _watch(sequence: list[Identity], on_lost: Recorder) -> ParentWatch:
    """A watch whose lookups return `sequence` in order, then repeat the last."""
    remaining = list(sequence)

    def identify(_pid: int) -> Identity:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return ParentWatch(4242, on_lost, poll_interval_s=0.01, identify=identify)


def test_alive_while_the_creation_time_matches() -> None:
    lost = Recorder()
    watch = _watch([100.0], lost)

    assert watch.is_alive() is True
    assert watch.check_once() is True
    assert lost.calls == 0


def test_a_reused_pid_is_not_the_supervisor() -> None:
    """A different process wearing the same PID must not pass for MAIN."""
    lost = Recorder()
    watch = _watch([100.0, 200.0], lost)

    assert watch.is_alive() is False
    assert watch.check_once() is False
    assert lost.calls == 1


def test_a_dead_supervisor_is_lost() -> None:
    lost = Recorder()
    watch = _watch([100.0, GONE], lost)

    assert watch.check_once() is False
    assert lost.calls == 1


def test_a_refused_lookup_is_not_a_death() -> None:
    """`AccessDenied` means the process is there; the core must not exit on it."""
    lost = Recorder()
    watch = _watch([100.0, UNIDENTIFIED], lost)

    assert watch.is_alive() is True
    assert lost.calls == 0


def test_an_unidentifiable_supervisor_at_startup_stays_trusted() -> None:
    """With no baseline, identity cannot be proven — only a death counts."""
    lost = Recorder()
    watch = _watch([UNIDENTIFIED, 100.0], lost)

    assert watch.is_alive() is True
    assert lost.calls == 0


def test_a_supervisor_already_gone_at_startup_is_lost() -> None:
    lost = Recorder()
    watch = _watch([GONE], lost)

    assert watch.check_once() is False
    assert lost.calls == 1


def test_the_thread_fires_on_lost_and_stops_itself() -> None:
    lost = Recorder()
    state: list[Identity] = [100.0]

    def identify(_pid: int) -> Identity:
        return state[0]

    watch = ParentWatch(4242, lost, poll_interval_s=0.01, identify=identify)
    watch.start()
    time.sleep(0.05)
    assert lost.calls == 0

    state[0] = GONE
    time.sleep(0.1)
    watch.stop()

    # Once, not once per poll: the core only gets to be told to die a single time.
    assert lost.calls == 1


def test_stop_is_safe_before_and_after_start() -> None:
    watch = _watch([100.0], Recorder())

    watch.stop()
    watch.start()
    watch.start()  # idempotent
    watch.stop()
    watch.stop()


def test_process_identity_reads_a_real_process() -> None:
    me = psutil.Process()

    assert process_identity(me.pid) == me.create_time()


def test_process_identity_reports_a_dead_process_gone() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.wait(timeout=30)
    # Popen keeps no handle open on Windows once waited, so the PID is free.
    assert process_identity(child.pid) is GONE


def test_a_real_killed_supervisor_is_detected_inside_the_budget() -> None:
    """Kill a live process and the watch must notice, well inside the 2 s budget."""
    supervisor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lost = Recorder()
    watch = ParentWatch(supervisor.pid, lost)
    try:
        watch.start()
        time.sleep(0.6)
        assert lost.calls == 0

        killed_at = time.monotonic()
        supervisor.kill()
        supervisor.wait(timeout=30)
        while lost.calls == 0 and time.monotonic() - killed_at < STEP_6_BUDGET_S:
            time.sleep(0.01)
        noticed_after = time.monotonic() - killed_at
    finally:
        watch.stop()
        if supervisor.poll() is None:
            supervisor.kill()

    assert lost.calls == 1
    # The shutdown itself needs the rest of the budget, so noticing has to be fast.
    assert noticed_after < 1.0
