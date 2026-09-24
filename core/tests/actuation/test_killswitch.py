"""The core's half of the kill switch (`P3-06`): freeze, release, stay engaged.

MAIN's half — the hotkey, the deadline, terminating a core that does not answer —
is tested in `apps/desktop/tests/kill-switch.test.ts`.
"""

from __future__ import annotations

import gc
import threading
import time

import pytest
from aegis_core.actuation.input import (
    VK_LCONTROL,
    VK_LSHIFT,
    VK_RMENU,
    InputAbortedError,
    InputController,
    MouseButton,
)
from aegis_core.actuation.killswitch import KillSwitch

from tests.actuation.helpers import RecordingBackend

VK_A = 0x41


@pytest.fixture
def switch() -> KillSwitch:
    return KillSwitch()


def controller(switch: KillSwitch, backend: RecordingBackend | None = None) -> InputController:
    return InputController(backend or RecordingBackend(), kill=switch, inter_event_delay=0.0)


def test_a_new_switch_is_not_engaged(switch: KillSwitch) -> None:
    assert not switch.engaged


def test_engaging_releases_every_modifier_and_button_held(switch: KillSwitch) -> None:
    backend = RecordingBackend()
    ctl = controller(switch, backend)
    for vk in (VK_LCONTROL, VK_LSHIFT, VK_RMENU, VK_A):
        ctl.key_down(vk)
    ctl.button_down(MouseButton.LEFT)

    report = switch.engage()

    assert switch.engaged
    assert ctl.held_keys == ()
    assert ctl.held_buttons == ()
    assert (report.released_keys, report.released_buttons, report.release_failures) == (4, 1, 0)
    ups = [e.detail for e in backend.events if e.kind == "key" and e.down is False]
    # The plain key first, then the modifiers: an app must not see a bare `A`.
    assert ups[0] == VK_A
    assert set(ups[1:]) == {VK_LCONTROL, VK_LSHIFT, VK_RMENU}


def test_engaging_sweeps_every_attached_controller(switch: KillSwitch) -> None:
    first, second = controller(switch), controller(switch)
    first.key_down(VK_LCONTROL)
    second.key_down(VK_LSHIFT)

    assert switch.engage().released_keys == 2
    assert first.held_keys == second.held_keys == ()


def test_a_failing_controller_does_not_strand_the_others(switch: KillSwitch) -> None:
    # Two broken ones: the sweep order is a weak set's, so with only one the
    # failure could happen to come last and an early exit would go unseen.
    broken = [
        controller(switch, RecordingBackend(fail_keys=frozenset({VK_LCONTROL}))) for _ in "ab"
    ]
    healthy = controller(switch)
    for ctl in broken:
        ctl.key_down(VK_LCONTROL)
    healthy.key_down(VK_LSHIFT)

    report = switch.engage()

    assert report.release_failures == 2
    assert healthy.held_keys == ()


def test_no_action_starts_once_engaged(switch: KillSwitch) -> None:
    backend = RecordingBackend()
    ctl = controller(switch, backend)
    switch.engage()

    with pytest.raises(InputAbortedError):
        ctl.tap(VK_A)
    with pytest.raises(InputAbortedError):
        ctl.click()
    assert backend.events == []


def test_engaging_aborts_a_sequence_in_flight_and_releases_what_it_held(
    switch: KillSwitch,
) -> None:
    backend = RecordingBackend(delay=0.005)
    ctl = controller(switch, backend)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            ctl.press_combo(VK_LCONTROL, VK_LSHIFT, VK_A)
            ctl.type_text("x" * 200)
        except BaseException as exc:  # recorded for the assertion
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.01)
    switch.engage()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], InputAbortedError)
    assert ctl.held_keys == ()
    chars = [e for e in backend.events if e.kind == "char"]
    assert len(chars) < 200


def test_a_key_pressed_after_the_sweep_is_released_before_the_call_returns(
    switch: KillSwitch,
) -> None:
    """`key_down()` has no checkpoint after its event, so the sweep could miss it."""
    backend = RecordingBackend()
    ctl = controller(switch, backend)
    swept = threading.Event()
    original = backend.key

    def key(vk: int, *, down: bool) -> None:
        original(vk, down=down)
        if down and not swept.is_set():
            # The sweep lands between this KEYDOWN and the registry update.
            swept.set()
            switch.engage()

    backend.key = key  # type: ignore[method-assign]

    with pytest.raises(InputAbortedError):
        ctl.key_down(VK_LCONTROL)
    assert ctl.held_keys == ()
    assert [(e.detail, e.down) for e in backend.events] == [
        (VK_LCONTROL, True),
        (VK_LCONTROL, False),
    ]


def test_the_switch_stays_engaged_until_reset(switch: KillSwitch) -> None:
    ctl = controller(switch)
    switch.engage()
    switch.engage()  # a second press sweeps again, harmlessly
    assert switch.engaged

    switch.reset()
    ctl.tap(VK_A)
    assert not switch.engaged


def test_the_switch_does_not_keep_a_controller_alive(switch: KillSwitch) -> None:
    controller(switch).key_down(VK_A)
    gc.collect()
    assert switch.engage().released_keys == 0


def test_a_controller_without_a_switch_is_unaffected() -> None:
    backend = RecordingBackend()
    InputController(backend, inter_event_delay=0.0).tap(VK_A)
    assert len(backend.events) == 2
