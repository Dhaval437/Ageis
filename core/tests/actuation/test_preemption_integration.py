"""Timed end-to-end preemption (`P3-05`), against real hooks.

The promise in `REMEMBER.md` invariant 1 and `UI.md § 7` is a wall-clock one:
from the moment the human touches the keyboard, the agent has 100 ms to have
stopped. This measures the whole path — Windows' input queue, the hook thread,
the signal, the action thread noticing, and every held modifier being released —
and fails if it does not fit in the budget.

The timed runs drive the action against a recording backend, so nothing is typed
into the developer's desktop; only the *trigger* is real input. One test goes
further and holds real modifiers in Windows through `SendInputBackend`, then
asks Windows itself whether any is still down after the takeover.
"""

from __future__ import annotations

import ctypes
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Win32 low-level hooks")

from aegis_core.actuation import win32  # noqa: E402
from aegis_core.actuation.input import (  # noqa: E402
    VK_LSHIFT,
    VK_RCONTROL,
    InputAbortedError,
    InputController,
    SendInputBackend,
)
from aegis_core.actuation.preempt import InputMonitor, PreemptSignal  # noqa: E402
from aegis_core.actuation.signature import AEGIS_SIGNATURE  # noqa: E402

from tests.actuation.helpers import (  # noqa: E402
    RecordingBackend,
    inject_untagged_key,
    settle,
    skip_if_walled_off,
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


# ---------------------------------------------------------------------------
# Real keys held in Windows, released by a real preemption.
# ---------------------------------------------------------------------------

#: F22: like F24, bound by nothing in a default Windows install.
VK_F22 = 0x85
#: One left, one right. `GetAsyncKeyState` follows the VK, not the scan code, so
#: this cannot see a wrong extended flag (`test_signature.py` covers that). No Win
#: key: releasing it on its own opens Start on the developer's desktop.
LIVE_HELD = (VK_LSHIFT, VK_RCONTROL)
#: How long Windows may take to show a `KEYUP` that `SendInput` already accepted.
OS_SETTLE_S = 0.1

#: A loader of our own, so the cleanup below shares no code with what it cleans
#: up after. In the 2026-09-26 incident (`REMEMBER.md § 9`) a broken release
#: path also broke the test's cleanup, and a Shift stayed down on the desktop.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short


def os_says_down(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def force_up(*vks: int) -> None:
    """Release `vks` through a raw `SendInput`, tagged so it cannot preempt."""
    for vk in vks:
        record = win32.INPUT(type=win32.INPUT_KEYBOARD)
        record.u.ki = win32.KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=win32.KEYEVENTF_KEYUP, time=0, dwExtraInfo=AEGIS_SIGNATURE
        )
        _user32.SendInput(1, ctypes.byref(record), ctypes.sizeof(win32.INPUT))


@dataclass
class LiveOutcome:
    #: Takeover to the action unwound, its `KEYUP`s accepted by `SendInput`.
    aborted_ms: float
    #: Takeover to Windows itself reporting every key up.
    keys_up_ms: float


def run_live_once(signal: PreemptSignal) -> LiveOutcome:
    """Hold real keys in Windows mid-action, take over, and ask Windows what is down."""
    held = (*LIVE_HELD, VK_F22)
    controller = InputController(SendInputBackend(), signal, inter_event_delay=5.0)
    aborted_at: list[float] = []
    error: list[BaseException] = []

    def act() -> None:
        try:
            for vk in LIVE_HELD:
                controller.key_down(vk)
            controller.tap(VK_F22)  # F22 down, then a 5 s pace the takeover cuts short
        except InputAbortedError:
            aborted_at.append(time.perf_counter())
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=act, name="aegis-action")
    thread.start()
    try:
        assert wait_until(lambda: VK_F22 in controller.held_keys), "the action never got going"
        assert wait_until(lambda: all(os_says_down(vk) for vk in held)), (
            "Windows never saw the keys go down, so this run proves nothing"
        )

        sent_at = time.perf_counter()
        inject_untagged_key()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the action never aborted"
        all_up = wait_until(lambda: not any(os_says_down(vk) for vk in held), timeout=OS_SETTLE_S)
        up_at = time.perf_counter()
        # Read before the cleanup below hides it.
        stuck = [hex(vk) for vk in held if os_says_down(vk)]
    finally:
        force_up(*reversed(held))

    if error:
        raise error[0]
    assert aborted_at, "the action finished without aborting"
    assert all_up, f"Windows still had keys down after the preemption: {stuck}"
    assert controller.held_keys == ()
    return LiveOutcome((aborted_at[0] - sent_at) * 1000, (up_at - sent_at) * 1000)


def test_real_modifiers_held_in_windows_are_up_after_a_real_preemption(
    signal: PreemptSignal,
) -> None:
    """Invariant 3 checked by Windows, not by our own bookkeeping, inside the budget."""
    skip_if_walled_off()
    if any(os_says_down(vk) for vk in (*LIVE_HELD, VK_F22)):
        pytest.skip("a key under test is already held on this machine")

    latencies: list[float] = []
    for _ in range(RUNS):
        assert settle(signal), "someone was typing during the run"
        latencies.append(run_live_once(signal).keys_up_ms)

    assert max(latencies) < BUDGET_MS, (
        f"keys were up {max(latencies):.1f} ms after the takeover (budget {BUDGET_MS:.0f} ms); "
        + "all runs: "
        + ", ".join(f"{value:.1f}" for value in latencies)
    )
