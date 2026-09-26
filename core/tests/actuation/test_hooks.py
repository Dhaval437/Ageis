"""`WH_MOUSE_LL` / `WH_KEYBOARD_LL` with real hooks on a real desktop (`P3-04`).

Two claims are load-bearing:

1. An event AEGIS synthesised (tagged `AEGIS_SIGNATURE`) does **not** preempt.
   Without this the agent pauses itself on its own first click.
2. An untagged event sets the signal inside the hook callback in under 5 ms.

**On isolation.** The hooks watch the whole machine, so a developer with a hand
on the mouse produces exactly the events under test. The tests therefore split
in two:

* the *decision* — which `dwExtraInfo` values preempt — is driven through the
  real callbacks with hand-built hook structs, deterministically, for both
  devices;
* the *integration* — that the hooks really are installed and really do fire —
  runs on the keyboard only, injecting `VK_F24`, which nothing in a default
  Windows install binds and nobody presses by accident. Mouse noise cannot
  reach a keyboard-only monitor, so those runs stay stable while the machine is
  in use.

One mouse test does drive the real queue end to end; it skips, loudly, if the
machine's mouse is not idle.
"""

from __future__ import annotations

import ctypes
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Win32 low-level hooks")

from aegis_core.actuation import win32  # noqa: E402
from aegis_core.actuation.input import SendInputBackend  # noqa: E402
from aegis_core.actuation.preempt import InputMonitor, PreemptEvent, PreemptSignal  # noqa: E402
from aegis_core.actuation.signature import AEGIS_SIGNATURE  # noqa: E402

from tests.actuation import helpers  # noqa: E402
from tests.actuation.helpers import (  # noqa: E402
    VK_F24,
    expect_preempted,
    inject_untagged_key,
    settle,
)

#: Injections per latency sample. Enough to be meaningful, quick enough to run
#: on every commit.
SAMPLES = 15
#: `ARCHITECTURE.md § 8.3`: the callback sets the event within 5 ms.
CALLBACK_BUDGET_MS = 5.0
#: How long to give Windows to deliver a hook callback before deciding it never
#: will. Generous on purpose: a false "not preempted" is the dangerous failure,
#: so the negative assertions must wait properly rather than sample once.
DELIVERY_S = 0.25
#: Retries for the one test a busy mouse can make inconclusive.
ATTEMPTS = 8

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A


@pytest.fixture
def signal() -> PreemptSignal:
    return PreemptSignal()


@pytest.fixture
def keys_only(signal: PreemptSignal) -> Iterator[InputMonitor]:
    """A monitor deaf to the mouse, so a moving cursor cannot pollute the result."""
    with InputMonitor(signal, watch_mouse=False) as monitor:
        yield monitor


def is_running(monitor: InputMonitor) -> bool:
    """Read the property through a call so mypy does not narrow it across start/stop."""
    return monitor.running


# ---------------------------------------------------------------------------
# The decision: which events preempt. Driven through the real callbacks.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `CallNextHookEx`; these tests call the callbacks outside a hook chain."""
    monkeypatch.setattr(win32.user32, "CallNextHookEx", lambda *args: 0)


def deliver_key(monitor: InputMonitor, *, extra_info: int, vk: int = VK_F24) -> None:
    info = win32.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=extra_info)
    monitor._on_keyboard(win32.HC_ACTION, win32.WM_KEYDOWN, ctypes.addressof(info))


def deliver_mouse(
    monitor: InputMonitor,
    *,
    extra_info: int,
    message: int = win32.WM_MOUSEMOVE,
    x: int = 500,
    y: int = 500,
) -> None:
    info = win32.MSLLHOOKSTRUCT(
        pt=wintypes.POINT(x, y), mouseData=0, flags=0, time=0, dwExtraInfo=extra_info
    )
    monitor._on_mouse(win32.HC_ACTION, message, ctypes.addressof(info))


@pytest.mark.usefixtures("no_chain")
def test_a_tagged_keystroke_does_not_preempt(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    deliver_key(monitor, extra_info=AEGIS_SIGNATURE)
    assert not signal.is_set()
    assert monitor.callback_errors == 0


@pytest.mark.usefixtures("no_chain")
def test_an_untagged_keystroke_preempts(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    deliver_key(monitor, extra_info=0)
    assert signal.is_set()
    event = signal.last
    assert event is not None
    assert event.source == "keyboard"
    assert monitor.callback_errors == 0


@pytest.mark.usefixtures("no_chain")
def test_a_tagged_mouse_event_does_not_preempt(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    for message in (win32.WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL):
        deliver_mouse(monitor, extra_info=AEGIS_SIGNATURE, message=message, x=600, y=400)
    assert not signal.is_set()
    assert monitor.callback_errors == 0


@pytest.mark.usefixtures("no_chain")
def test_an_untagged_mouse_event_preempts(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    deliver_mouse(monitor, extra_info=0)
    assert signal.is_set()
    event = signal.last
    assert event is not None
    assert event.source == "mouse"


@pytest.mark.usefixtures("no_chain")
def test_a_near_miss_signature_still_preempts(signal: PreemptSignal) -> None:
    """The comparison is exact. Anything that is not our tag is the human."""
    monitor = InputMonitor(signal)
    deliver_key(monitor, extra_info=AEGIS_SIGNATURE + 1)
    assert signal.is_set()


@pytest.mark.usefixtures("no_chain")
def test_the_jitter_deadzone_filters_only_small_moves(signal: PreemptSignal) -> None:
    """Opt-in knob for noisy drivers. It never filters a click, and defaults to off."""
    monitor = InputMonitor(signal, mouse_move_threshold_px=5)
    deliver_mouse(monitor, extra_info=0, x=500, y=500)  # establishes the origin
    signal.clear()

    deliver_mouse(monitor, extra_info=0, x=502, y=501)
    assert not signal.is_set(), "a 3 px twitch should sit inside a 5 px deadzone"

    deliver_mouse(monitor, extra_info=0, message=WM_LBUTTONDOWN, x=502, y=501)
    assert signal.is_set(), "a click must never be filtered as jitter"
    signal.clear()

    deliver_mouse(monitor, extra_info=0, x=560, y=560)
    assert signal.is_set(), "a real move must preempt"


@pytest.mark.usefixtures("no_chain")
def test_the_default_has_no_deadzone_at_all(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    deliver_mouse(monitor, extra_info=0, x=500, y=500)
    signal.clear()
    deliver_mouse(monitor, extra_info=0, x=501, y=500)
    assert signal.is_set(), "the default must react to a single pixel"


# ---------------------------------------------------------------------------
# The integration: the hooks are really installed and really do fire.
# ---------------------------------------------------------------------------


def test_the_monitor_installs_and_removes_its_hooks(signal: PreemptSignal) -> None:
    monitor = InputMonitor(signal)
    assert not is_running(monitor)
    monitor.start()
    assert is_running(monitor)
    monitor.stop()
    assert not is_running(monitor)
    monitor.stop()  # idempotent
    assert monitor.callback_errors == 0


def test_synthetic_keystrokes_do_not_preempt(
    signal: PreemptSignal, keys_only: InputMonitor
) -> None:
    """The whole preemption design rests on this one, through the real input queue."""
    # Behind an elevated window nothing is delivered, and silence would pass this.
    helpers.skip_if_walled_off()
    backend = SendInputBackend()
    assert settle(signal), "someone was typing during the run"

    for _ in range(SAMPLES):
        backend.key(VK_F24, down=True)
        backend.key(VK_F24, down=False)

    assert not signal.wait(DELIVERY_S), f"a tagged keystroke preempted AEGIS: {signal.last}"
    assert keys_only.callback_errors == 0


def test_untagged_input_preempts(signal: PreemptSignal, keys_only: InputMonitor) -> None:
    assert settle(signal), "someone was typing during the run"
    sent_at = time.perf_counter()
    inject_untagged_key()

    expect_preempted(signal, keys_only, sent_at, DELIVERY_S)
    event = signal.last
    assert event is not None
    assert event.source == "keyboard"
    assert keys_only.callback_errors == 0


def test_the_callback_sets_the_signal_within_5ms(
    signal: PreemptSignal, keys_only: InputMonitor
) -> None:
    """Measured from just before injection to the timestamp taken in the callback."""
    latencies_ms: list[float] = []

    for _ in range(SAMPLES):
        assert settle(signal), "someone was typing during the run"
        sent_at = time.perf_counter()
        inject_untagged_key()
        expect_preempted(signal, keys_only, sent_at, DELIVERY_S)
        event = signal.last
        assert event is not None
        latencies_ms.append((event.at - sent_at) * 1000)

    median = statistics.median(latencies_ms)
    assert median < CALLBACK_BUDGET_MS, f"median {median:.2f} ms over budget: {latencies_ms}"
    # A single outlier is the OS scheduler, not a design fault — but it must
    # still be nowhere near the 100 ms the user is promised.
    assert max(latencies_ms) < 50.0, f"worst case {max(latencies_ms):.2f} ms: {latencies_ms}"
    assert keys_only.callback_errors == 0


def test_synthetic_mouse_events_do_not_preempt_on_an_idle_machine(
    signal: PreemptSignal,
) -> None:
    """The mouse half of the same claim, through the real queue. Needs a still mouse.

    A run in which the human's own cursor moved proves nothing either way, so it
    is retried rather than failed. `test_a_tagged_mouse_event_does_not_preempt`
    is the guard that cannot be skipped; this one adds the real input queue when
    the machine is quiet enough to offer it.
    """
    helpers.skip_if_walled_off()  # silence would pass this too
    backend = SendInputBackend()
    with InputMonitor(signal) as monitor:
        for _ in range(ATTEMPTS):
            if not settle(signal):
                continue
            for _ in range(SAMPLES):
                backend.mouse_move(1, 0)
                backend.mouse_move(-1, 0)
            if not signal.wait(DELIVERY_S):
                assert monitor.callback_errors == 0
                return
        pytest.skip("the mouse never held still; the callback-level test still covers this")


def test_signature_is_configurable_and_is_what_the_hook_compares(
    signal: PreemptSignal,
) -> None:
    """Point the monitor at a signature we never send: our own events now preempt."""
    helpers.skip_if_walled_off()
    with InputMonitor(signal, watch_mouse=False, signature=0x0BADF00D) as monitor:
        assert settle(signal), "someone was typing during the run"
        backend = SendInputBackend()
        backend.key(VK_F24, down=True)
        backend.key(VK_F24, down=False)  # never leave a key down (invariant 3)

        # Not `expect_preempted`: its second, untagged key would get through a hook
        # that ignores the configured signature and blame the first on "eaten".
        assert signal.wait(DELIVERY_S), "the hook is not comparing the configured signature"
        assert monitor.callback_errors == 0


def test_listeners_run_off_the_hook_thread(signal: PreemptSignal, keys_only: InputMonitor) -> None:
    """Reacting to preemption is I/O; it must not happen inside the callback."""
    seen: list[str] = []
    signal.add_listener(lambda event: seen.append(threading.current_thread().name))

    assert settle(signal), "someone was typing during the run"
    with signal.watch():
        sent_at = time.perf_counter()
        inject_untagged_key()
        expect_preempted(signal, keys_only, sent_at, DELIVERY_S)
        deadline = time.perf_counter() + 2.0
        while not seen and time.perf_counter() < deadline:
            time.sleep(0.005)

    assert seen == ["aegis-preempt-watch"]


# ---------------------------------------------------------------------------
# When a live test misses: the miss must name its cause (P3-16).
# ---------------------------------------------------------------------------


class _Stub:
    """A monitor as `explain_miss` sees it: only whether its thread is alive."""

    def __init__(self, running: bool) -> None:
        self.running = running


@pytest.fixture
def focus_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule out UIPI, so each test reaches the branch it is about."""
    monkeypatch.setattr(helpers, "foreground_outranks_us", lambda: None)


def test_a_miss_behind_an_elevated_window_is_blamed_on_uipi(
    monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal
) -> None:
    monkeypatch.setattr(helpers, "foreground_outranks_us", lambda: "'Task Manager' (pid 1)")
    cause, environmental = helpers.explain_miss(signal, _Stub(running=True), time.perf_counter())
    assert environmental
    assert "UIPI" in cause and "Task Manager" in cause


def test_an_elevated_window_skips_rather_than_fails(
    monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal
) -> None:
    monkeypatch.setattr(helpers, "foreground_outranks_us", lambda: "'Task Manager' (pid 1)")
    with pytest.raises(pytest.skip.Exception, match="UIPI"):
        helpers.expect_preempted(signal, _Stub(running=True), time.perf_counter(), 0.01)


@pytest.mark.usefixtures("focus_is_ours")
def test_a_late_key_is_blamed_on_a_starved_thread(signal: PreemptSignal) -> None:
    sent_at = time.perf_counter()
    late = threading.Timer(
        0.3, signal.trigger, (PreemptEvent("keyboard", win32.WM_KEYDOWN, sent_at + 0.3),)
    )
    late.start()
    try:
        cause, environmental = helpers.explain_miss(signal, _Stub(running=True), sent_at)
    finally:
        late.cancel()
    assert not environmental
    assert "300 ms late" in cause and "starved" in cause


@pytest.mark.usefixtures("focus_is_ours")
def test_a_dead_thread_is_named(monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal) -> None:
    monkeypatch.setattr(helpers, "LATE_S", 0.01)
    cause, environmental = helpers.explain_miss(signal, _Stub(running=False), time.perf_counter())
    assert not environmental
    assert cause == "the hook thread is dead"


def test_a_key_that_gets_through_the_second_time_was_eaten(
    monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal, keys_only: InputMonitor
) -> None:
    """A real hook, and a first key that "never arrived": the second one does."""
    helpers.skip_if_walled_off()  # the real check, before it is stubbed out below
    monkeypatch.setattr(helpers, "foreground_outranks_us", lambda: None)
    monkeypatch.setattr(helpers, "LATE_S", 0.01)
    assert settle(signal), "someone was typing during the run"
    cause, environmental = helpers.explain_miss(signal, keys_only, time.perf_counter())
    assert not environmental
    assert "eaten" in cause


@pytest.mark.usefixtures("focus_is_ours")
def test_a_hook_that_no_longer_fires_is_named(
    monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal
) -> None:
    """No hook behind this signal at all: the second key is lost too."""
    monkeypatch.setattr(helpers, "LATE_S", 0.01)
    cause, environmental = helpers.explain_miss(signal, _Stub(running=True), time.perf_counter())
    assert not environmental
    assert "no longer fires" in cause


@pytest.mark.usefixtures("focus_is_ours")
def test_a_miss_that_is_ours_fails_with_its_cause(
    monkeypatch: pytest.MonkeyPatch, signal: PreemptSignal
) -> None:
    monkeypatch.setattr(helpers, "LATE_S", 0.01)
    with pytest.raises(pytest.fail.Exception, match="within 10 ms: the hook thread is dead"):
        helpers.expect_preempted(signal, _Stub(running=False), time.perf_counter(), 0.01)


def test_our_own_integrity_is_readable_and_ordinary() -> None:
    """The comparison rests on reading a token; ours must read as medium or high."""
    import os

    level = helpers.process_integrity(os.getpid())
    assert level in (helpers.MEDIUM_INTEGRITY, helpers.HIGH_INTEGRITY)


def test_a_process_that_does_not_exist_has_no_integrity() -> None:
    assert helpers.process_integrity(0xFFFFFFF0) is None


@pytest.mark.parametrize(
    ("theirs", "walled_off"),
    [
        (helpers.MEDIUM_INTEGRITY, False),
        (helpers.MEDIUM_INTEGRITY - 0x1000, False),
        (helpers.HIGH_INTEGRITY, True),
        (None, True),
    ],
    ids=["same", "lower", "higher", "unreadable"],
)
def test_only_a_window_that_outranks_us_is_walled_off(
    monkeypatch: pytest.MonkeyPatch, theirs: int | None, walled_off: bool
) -> None:
    import os

    ours = os.getpid()
    monkeypatch.setattr(
        helpers,
        "process_integrity",
        lambda pid: helpers.MEDIUM_INTEGRITY if pid == ours else theirs,
    )
    assert (helpers.foreground_outranks_us() is not None) == walled_off
