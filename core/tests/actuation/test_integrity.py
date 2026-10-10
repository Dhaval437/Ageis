"""Acting while a window that outranks us has focus (`P3-20`).

Measured with Task Manager in front: `SendInput` reports success and nothing
lands, the hooks see neither our input nor the human's, and a key pressed before
the window came forward stays down because its `KEYUP` is dropped too. These
tests hold `InputController` to what follows from that: refuse and pause, and
owe — then re-send — every release Windows may have dropped.

`RecordingBackend.blocked` stands the elevated window in front: events are still
recorded (they were sent), but "Windows" key state does not change.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 integrity levels")

from aegis_core.actuation import integrity, win32  # noqa: E402
from aegis_core.actuation.input import (  # noqa: E402
    VK_LCONTROL,
    VK_LSHIFT,
    InputAbortedError,
    InputController,
    MouseButton,
    WalledOffError,
)
from aegis_core.actuation.integrity import (  # noqa: E402
    HIGH_INTEGRITY,
    MEDIUM_INTEGRITY,
    Blocker,
    foreground_blocker,
)
from aegis_core.actuation.killswitch import KillSwitch  # noqa: E402
from aegis_core.actuation.preempt import PreemptEvent, PreemptSignal  # noqa: E402

from tests.actuation.helpers import Recorded, RecordingBackend, wait_until  # noqa: E402

TASK_MANAGER = Blocker(pid=4242, integrity=HIGH_INTEGRITY, image="Taskmgr.exe")
NO_FOREGROUND = Blocker(pid=0, integrity=None, image=None)
VK_A = 0x41


@pytest.fixture
def signal() -> PreemptSignal:
    return PreemptSignal()


@pytest.fixture
def backend() -> RecordingBackend:
    return RecordingBackend()


def controller(backend: RecordingBackend, signal: PreemptSignal | None) -> InputController:
    return InputController(backend, signal, inter_event_delay=0)


def block_after(backend: RecordingBackend, count: int, blocker: Blocker = TASK_MANAGER) -> None:
    """Bring the elevated window forward right after the `count`-th event is sent."""
    seen = 0

    def on_event(_: Recorded) -> None:
        nonlocal seen
        seen += 1
        if seen == count:
            backend.blocked = blocker

    backend.on_event = on_event


# ---------------------------------------------------------------------------
# Reading integrity levels
# ---------------------------------------------------------------------------


def test_our_own_integrity_is_readable_and_ordinary() -> None:
    """The comparison rests on reading a token; ours must read as medium or high."""
    assert win32.process_integrity(os.getpid()) in (MEDIUM_INTEGRITY, HIGH_INTEGRITY)


def test_a_process_that_does_not_exist_has_no_integrity_and_no_name() -> None:
    assert win32.process_integrity(0xFFFFFFF0) is None
    assert win32.process_image_name(0xFFFFFFF0) is None


def test_a_program_is_named_by_its_file_alone() -> None:
    name = win32.process_image_name(os.getpid())
    assert name is not None
    assert name.lower().endswith(".exe") and "\\" not in name


@pytest.mark.parametrize(
    ("theirs", "walled_off"),
    [
        (MEDIUM_INTEGRITY, False),
        (MEDIUM_INTEGRITY - 0x1000, False),
        (HIGH_INTEGRITY, True),
        (None, True),
    ],
    ids=["same", "lower", "higher", "unreadable"],
)
def test_only_a_window_that_outranks_us_is_walled_off(
    monkeypatch: pytest.MonkeyPatch, theirs: int | None, walled_off: bool
) -> None:
    monkeypatch.setattr(win32, "foreground_pid", lambda: 4242)
    monkeypatch.setattr(win32, "process_integrity", lambda pid: theirs)
    monkeypatch.setattr(win32, "process_image_name", lambda pid: "Taskmgr.exe")
    monkeypatch.setattr(integrity, "own_integrity", lambda: MEDIUM_INTEGRITY)
    blocker = foreground_blocker()
    assert (blocker is not None) == walled_off
    if blocker is not None:
        assert blocker == Blocker(pid=4242, integrity=theirs, image="Taskmgr.exe")


def test_our_own_unreadable_level_counts_as_walled_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """If we cannot tell, we cannot promise our input lands."""
    monkeypatch.setattr(win32, "foreground_pid", lambda: 4242)
    monkeypatch.setattr(win32, "process_integrity", lambda pid: MEDIUM_INTEGRITY)
    monkeypatch.setattr(win32, "process_image_name", lambda pid: None)
    monkeypatch.setattr(integrity, "own_integrity", lambda: None)
    assert foreground_blocker() is not None


def test_no_window_in_front_is_its_own_kind_of_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win32, "foreground_pid", lambda: None)
    blocker = foreground_blocker()
    assert blocker is not None and blocker.no_foreground
    assert "no window has focus" in blocker.describe()


def test_a_blocker_is_described_without_a_title_or_a_path() -> None:
    assert TASK_MANAGER.describe() == "Taskmgr.exe (pid 4242, integrity 0x3000)"
    assert "unreadable" in Blocker(pid=7, integrity=None, image=None).describe()


# ---------------------------------------------------------------------------
# Refusing, and pausing
# ---------------------------------------------------------------------------


def test_nothing_is_sent_while_a_window_that_outranks_us_has_focus(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    backend.blocked = TASK_MANAGER
    with pytest.raises(WalledOffError) as caught:
        controller(backend, signal).type_text("hello")
    assert backend.events == []
    assert caught.value.blocker == TASK_MANAGER
    assert "Taskmgr.exe" in str(caught.value)


def test_the_wall_pauses_the_task_like_a_takeover(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    """Behind it the hooks are blind, so carrying on after it closes would be acting blind."""
    backend.blocked = TASK_MANAGER
    with pytest.raises(WalledOffError):
        controller(backend, signal).tap(VK_A)
    assert signal.is_set()
    assert signal.last is not None and signal.last.source == "walled_off"


def test_a_walled_off_action_is_an_aborted_one(backend: RecordingBackend) -> None:
    """Callers that handle a takeover handle this too, without knowing about UIPI."""
    backend.blocked = TASK_MANAGER
    with pytest.raises(InputAbortedError):
        controller(backend, None).tap(VK_A)


def test_a_window_that_comes_forward_mid_sequence_stops_it(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    block_after(backend, 3)
    with pytest.raises(WalledOffError):
        controller(backend, signal).type_text("x" * 50)
    assert backend.kinds().count("char") == 3


def test_a_window_that_comes_forward_after_the_last_event_fails_the_action(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    """The final event may have been dropped, so the action cannot count as done."""
    block_after(backend, 1)
    with pytest.raises(WalledOffError):
        controller(backend, signal).scroll(dy=-3)


def test_a_moment_with_no_window_in_front_is_waited_out(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    """Focus passes through "nobody" between two windows; that is not a wall."""
    backend.blocked = NO_FOREGROUND
    threading.Timer(0.05, setattr, (backend, "blocked", None)).start()
    controller(backend, signal).tap(VK_A)
    assert backend.key_downs() == [VK_A]
    assert not signal.is_set()


def test_a_lasting_absence_of_any_window_is_a_wall(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    """A UAC prompt's secure desktop, or the lock screen."""
    backend.blocked = NO_FOREGROUND
    started = time.monotonic()
    with pytest.raises(WalledOffError) as caught:
        controller(backend, signal).tap(VK_A)
    assert caught.value.blocker.no_foreground
    assert time.monotonic() - started >= 0.2
    assert backend.events == []


def test_a_takeover_during_that_wait_is_honoured_at_once(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    backend.blocked = NO_FOREGROUND
    takeover = PreemptEvent("keyboard", win32.WM_KEYDOWN, time.perf_counter())
    threading.Timer(0.03, signal.trigger, (takeover,)).start()
    started = time.monotonic()
    with pytest.raises(InputAbortedError) as caught:
        controller(backend, signal).tap(VK_A)
    assert not isinstance(caught.value, WalledOffError)
    assert time.monotonic() - started < 0.2


# ---------------------------------------------------------------------------
# Owed releases: invariant 3 behind the wall
# ---------------------------------------------------------------------------


def test_keys_released_behind_the_wall_are_owed(
    backend: RecordingBackend, signal: PreemptSignal
) -> None:
    """The measured case: held before the window came forward, KEYUP dropped."""
    ctl = controller(backend, signal)
    ctl.key_down(VK_LCONTROL)
    ctl.key_down(VK_LSHIFT)
    block_after(backend, 3)  # after the first char
    with pytest.raises(WalledOffError) as caught:
        ctl.type_text("abc")
    assert set(caught.value.released) == {VK_LCONTROL, VK_LSHIFT}  # the KEYUPs were sent…
    assert backend.down == {VK_LCONTROL, VK_LSHIFT}  # …and Windows dropped them
    assert ctl.owed == (frozenset({VK_LCONTROL, VK_LSHIFT}), frozenset())
    assert ctl.held_keys == ()


def test_owed_releases_wait_while_the_wall_stands(backend: RecordingBackend) -> None:
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    sent = len(backend.events)
    assert ctl.settle_owed() is False
    assert len(backend.events) == sent
    assert backend.down == {VK_LCONTROL}


def test_owed_releases_are_sent_again_once_the_wall_falls(backend: RecordingBackend) -> None:
    ctl = controller(backend, None)
    ctl.key_down(VK_A)
    ctl.key_down(VK_LCONTROL)
    ctl.button_down(MouseButton.LEFT)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    backend.on_event = None
    backend.blocked = None
    before = len(backend.events)

    assert ctl.settle_owed() is True
    resent = backend.events[before:]
    assert [(e.kind, e.detail, e.down) for e in resent] == [
        ("button", MouseButton.LEFT, False),
        ("key", VK_A, False),
        ("key", VK_LCONTROL, False),  # modifiers last, as in release_all()
    ]
    assert backend.down == set()
    assert ctl.owed == (frozenset(), frozenset())


def test_the_retry_thread_settles_them_without_being_asked(backend: RecordingBackend) -> None:
    """The task may be paused for an hour; the stuck Ctrl may not wait for its next action."""
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    time.sleep(0.1)
    assert backend.down == {VK_LCONTROL}  # still walled: nothing it could do
    backend.blocked = None
    assert wait_until(lambda: ctl.owed == (frozenset(), frozenset()), timeout=2.0)
    assert backend.down == set()


def test_only_what_windows_still_has_down_is_released_again(backend: RecordingBackend) -> None:
    """The human may have pressed and released it since; a stray KEYUP is not needed."""
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    backend.down.discard(VK_LCONTROL)
    backend.blocked = None
    before = len(backend.events)
    assert ctl.settle_owed() is True
    assert backend.events[before:] == []


def test_focus_moving_again_during_the_retry_keeps_the_debt(backend: RecordingBackend) -> None:
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    backend.blocked = None
    block_after(backend, 1)  # the retried KEYUP itself goes out behind a new wall
    assert ctl.settle_owed() is False
    assert ctl.owed[0] == frozenset({VK_LCONTROL})


def test_a_new_action_settles_the_debt_before_it_presses_anything(
    backend: RecordingBackend,
) -> None:
    """Otherwise the retry thread could let go of a key the new action is holding."""
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    backend.blocked = TASK_MANAGER
    ctl.release_all()
    backend.blocked = None
    before = len(backend.events)
    ctl.key_down(VK_LCONTROL)
    assert [(e.detail, e.down) for e in backend.events[before:]] == [
        (VK_LCONTROL, False),
        (VK_LCONTROL, True),
    ]
    assert ctl.owed == (frozenset(), frozenset())
    assert ctl.held_keys == (VK_LCONTROL,)
    time.sleep(0.1)  # and the retry thread, if still around, leaves the hold alone
    assert backend.down == {VK_LCONTROL}


def test_a_kill_behind_the_wall_owes_what_it_released(backend: RecordingBackend) -> None:
    switch = KillSwitch()
    ctl = InputController(backend, kill=switch, inter_event_delay=0)
    ctl.key_down(VK_LSHIFT)
    backend.blocked = TASK_MANAGER
    report = switch.engage()
    assert report.released_keys == 1
    assert ctl.owed[0] == frozenset({VK_LSHIFT})
    backend.blocked = None
    assert wait_until(lambda: backend.down == set(), timeout=2.0)


def test_a_release_with_the_wall_down_owes_nothing(backend: RecordingBackend) -> None:
    ctl = controller(backend, None)
    ctl.key_down(VK_LCONTROL)
    ctl.release_all()
    assert ctl.owed == (frozenset(), frozenset())
    assert backend.down == set()
