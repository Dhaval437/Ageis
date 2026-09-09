"""Aborting an in-flight action, and the P0 rule that no held key survives it.

`REMEMBER.md` invariant 3: *every* abort path releases Ctrl/Alt/Shift/Win, both
sides. A stuck modifier is a P0 bug, so it gets its own explicit tests here
rather than being implied by a happy-path assertion.
"""

from __future__ import annotations

import threading

import pytest
from aegis_core.actuation.input import (
    MODIFIER_VKS,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_RCONTROL,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    InputAbortedError,
    InputController,
    MouseButton,
    ReleaseFailedError,
)
from aegis_core.actuation.preempt import PreemptEvent, PreemptSignal

from tests.actuation.helpers import RecordingBackend

#: Ctrl / Alt / Shift / Win, both sides. The exact set invariant 3 names.
BOTH_SIDED_MODIFIERS = (
    VK_LCONTROL,
    VK_RCONTROL,
    VK_LMENU,
    VK_RMENU,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_LWIN,
    VK_RWIN,
)

VK_A = 0x41


def preempt_now(signal: PreemptSignal) -> None:
    signal.trigger(PreemptEvent("keyboard", 0x0100, 0.0))


@pytest.fixture
def signal() -> PreemptSignal:
    return PreemptSignal()


def test_every_both_sided_modifier_is_named_a_modifier() -> None:
    """The release path keys off MODIFIER_VKS; invariant 3 names these eight."""
    assert set(BOTH_SIDED_MODIFIERS) <= MODIFIER_VKS


def test_abort_releases_every_held_modifier(signal: PreemptSignal) -> None:
    """P0: eight modifiers down, the human takes over, eight KEYUPs come back."""
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    for vk in BOTH_SIDED_MODIFIERS:
        controller.key_down(vk)
    assert set(controller.held_modifiers) == set(BOTH_SIDED_MODIFIERS)

    preempt_now(signal)
    with pytest.raises(InputAbortedError) as caught:
        controller.type_text("this must not be typed")

    assert controller.held_keys == ()
    assert controller.held_modifiers == ()
    assert set(caught.value.released) == set(BOTH_SIDED_MODIFIERS)
    assert set(backend.key_ups()) == set(BOTH_SIDED_MODIFIERS)
    assert "char" not in backend.kinds()


def test_abort_mid_sequence_stops_part_way(signal: PreemptSignal) -> None:
    """The action stops between events, not after the sequence finishes."""
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    typed: list[str] = []
    original = backend.unicode_char

    def spy(char: str) -> None:
        typed.append(char)
        if len(typed) == 3:
            preempt_now(signal)
        original(char)

    backend.unicode_char = spy  # type: ignore[method-assign]

    with pytest.raises(InputAbortedError):
        controller.type_text("abcdefghijklmnop")

    assert len(typed) == 3, "typing continued past the preemption"


def test_abort_during_a_combo_releases_the_modifier_it_was_holding(
    signal: PreemptSignal,
) -> None:
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    original = backend.key

    def spy(vk: int, *, down: bool) -> None:
        original(vk, down=down)
        if vk == VK_LSHIFT and down:
            preempt_now(signal)

    backend.key = spy  # type: ignore[method-assign]

    with pytest.raises(InputAbortedError):
        controller.press_combo(VK_LCONTROL, VK_LSHIFT, VK_A)

    assert controller.held_keys == ()
    assert set(backend.key_ups()) == {VK_LCONTROL, VK_LSHIFT}
    assert VK_A not in backend.key_downs(), "the combo continued past the preemption"


def test_abort_releases_a_held_mouse_button(signal: PreemptSignal) -> None:
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    original = backend.mouse_move

    def spy(dx: int, dy: int) -> None:
        original(dx, dy)
        preempt_now(signal)

    backend.mouse_move = spy  # type: ignore[method-assign]

    with pytest.raises(InputAbortedError) as caught:
        controller.drag(120, 40, steps=8)

    assert controller.held_buttons == ()
    assert caught.value.released_buttons == (MouseButton.LEFT,)


def test_release_continues_after_one_failure_then_reports_it(signal: PreemptSignal) -> None:
    """One refused KEYUP must not strand the other seven modifiers."""
    backend = RecordingBackend(fail_keys=frozenset({VK_LWIN}))
    controller = InputController(backend, signal, inter_event_delay=0)

    for vk in BOTH_SIDED_MODIFIERS:
        controller.key_down(vk)
    preempt_now(signal)

    with pytest.raises(ReleaseFailedError):
        controller.type_text("x")

    released = set(backend.key_ups())
    assert released == set(BOTH_SIDED_MODIFIERS) - {VK_LWIN}
    assert controller.held_keys == (), "the registry must not keep keys it could not release"


def test_modifiers_are_released_last(signal: PreemptSignal) -> None:
    """Releasing Ctrl before the key it modifies would look like a bare keypress."""
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    controller.key_down(VK_LCONTROL)
    controller.key_down(VK_A)
    preempt_now(signal)

    with pytest.raises(InputAbortedError):
        controller.type_text("x")

    assert backend.key_ups() == [VK_A, VK_LCONTROL]


def test_a_new_action_after_preemption_refuses_and_releases(signal: PreemptSignal) -> None:
    """Preemption latches: a second action does not sneak past a set signal."""
    backend = RecordingBackend()
    controller = InputController(backend, signal, inter_event_delay=0)

    controller.key_down(VK_LCONTROL)
    preempt_now(signal)

    with pytest.raises(InputAbortedError):
        controller.click()
    assert controller.held_keys == ()
    assert backend.key_ups() == [VK_LCONTROL]

    with pytest.raises(InputAbortedError):
        controller.click()
    assert "button" not in backend.kinds()


def test_release_all_is_safe_from_another_thread() -> None:
    """The kill switch calls this while a sequence is still running."""
    backend = RecordingBackend(delay=0.0005)
    controller = InputController(backend, None, inter_event_delay=0)
    for vk in BOTH_SIDED_MODIFIERS:
        controller.key_down(vk)

    done = threading.Event()

    def killer() -> None:
        controller.release_all()
        done.set()

    thread = threading.Thread(target=killer)
    thread.start()
    controller.release_all()
    thread.join(timeout=5)

    assert done.is_set()
    assert controller.held_keys == ()
    # Released exactly once between the two callers, never twice.
    assert sorted(backend.key_ups()) == sorted(BOTH_SIDED_MODIFIERS)
