"""Every synthetic event carries `AEGIS_SIGNATURE` (`P3-02`).

The preemption hook treats an untagged event as the human. If any code path in
`SendInputBackend` ever forgets the tag, AEGIS preempts itself: the agent would
pause the moment it clicked anything. These tests are the guard on that.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 SendInput")

from aegis_core.actuation import win32  # noqa: E402
from aegis_core.actuation.input import (  # noqa: E402
    VK_LCONTROL,
    VK_RCONTROL,
    MouseButton,
    SendInputBackend,
)
from aegis_core.actuation.signature import AEGIS_SIGNATURE  # noqa: E402

#: The complete public surface of the backend. A new method here without a
#: matching case below fails `test_no_untagged_code_path_exists`.
BACKEND_METHODS = frozenset(
    {"key", "unicode_char", "mouse_move", "mouse_move_absolute", "mouse_button", "scroll"}
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[win32.INPUT]:
    """Intercept SendInput so the tests inspect records without moving anything."""
    records: list[win32.INPUT] = []

    def fake_send_input(events: list[win32.INPUT]) -> None:
        records.extend(events)

    monkeypatch.setattr(win32, "send_input", fake_send_input)
    return records


def extra_info(record: win32.INPUT) -> int:
    if record.type == win32.INPUT_KEYBOARD:
        return int(record.u.ki.dwExtraInfo)
    return int(record.u.mi.dwExtraInfo)


def test_no_untagged_code_path_exists() -> None:
    """Lock the surface: a new backend method must be added to these tests."""
    public = {
        name
        for name in vars(SendInputBackend)
        if not name.startswith("_") and callable(getattr(SendInputBackend, name))
    }
    assert public == BACKEND_METHODS


def test_every_event_type_is_tagged(captured: list[win32.INPUT]) -> None:
    backend = SendInputBackend()

    backend.key(VK_LCONTROL, down=True)
    backend.key(VK_LCONTROL, down=False)
    backend.unicode_char("é")
    backend.unicode_char("𝄞")  # astral: two surrogate events
    backend.unicode_char("\n")
    backend.mouse_move(3, -4)
    backend.mouse_move_absolute(32768, 1024)
    for button in MouseButton:
        backend.mouse_button(button, down=True)
        backend.mouse_button(button, down=False)
    backend.scroll(dx=1, dy=-1)

    assert captured, "the fixture captured nothing — the test would pass vacuously"
    # 0 is what an untagged third-party event carries; ours must never look like one.
    assert AEGIS_SIGNATURE
    assert all(extra_info(record) == AEGIS_SIGNATURE for record in captured)


def test_right_hand_modifiers_are_flagged_extended(captured: list[win32.INPUT]) -> None:
    """Right Ctrl without KEYEVENTF_EXTENDEDKEY arrives as left Ctrl in some apps."""
    backend = SendInputBackend()
    backend.key(VK_RCONTROL, down=False)
    backend.key(VK_LCONTROL, down=False)

    right, left = captured
    assert right.u.ki.dwFlags & win32.KEYEVENTF_EXTENDEDKEY
    assert not left.u.ki.dwFlags & win32.KEYEVENTF_EXTENDEDKEY
    assert right.u.ki.dwFlags & win32.KEYEVENTF_KEYUP


def test_unicode_char_sends_a_surrogate_pair(captured: list[win32.INPUT]) -> None:
    SendInputBackend().unicode_char("𝄞")
    scans = [record.u.ki.wScan for record in captured]
    assert scans == [0xD834, 0xD834, 0xDD1E, 0xDD1E]  # down/up for each surrogate


def test_an_absolute_move_spans_the_virtual_desktop(captured: list[win32.INPUT]) -> None:
    """Without VIRTUALDESK, 0..65535 spans the *primary* monitor only: on a second
    screen every absolute move would land on the wrong one."""
    SendInputBackend().mouse_move_absolute(100, 200)
    [record] = captured
    flags = record.u.mi.dwFlags
    wanted = win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_ABSOLUTE | win32.MOUSEEVENTF_VIRTUALDESK
    assert flags & wanted == wanted
    assert (record.u.mi.dx, record.u.mi.dy) == (100, 200)


@pytest.mark.parametrize(
    ("vk", "extended"),
    [
        (0x25, True),  # Left: MapVirtualKeyW reports the keypad-4 scan code for it
        (0x2E, True),  # Delete
        (0x24, True),  # Home
        (0x22, True),  # Page Down
        (0x5B, True),  # left Windows
        (0x6F, True),  # keypad divide
        (0xAD, True),  # volume mute (0xE0 prefix from Windows itself)
        (0x64, False),  # keypad 4: the same scan code as Left, and *not* extended
        (0x41, False),  # A
        (0x0D, False),  # Enter on the main block
    ],
)
def test_keys_carry_their_scan_code_and_extended_flag(
    captured: list[win32.INPUT], vk: int, extended: bool
) -> None:
    SendInputBackend().key(vk, down=True)
    [record] = captured
    assert bool(record.u.ki.dwFlags & win32.KEYEVENTF_EXTENDEDKEY) is extended
    assert record.u.ki.wScan == win32.scan_code(vk) & 0xFF
    assert record.u.ki.wScan != 0


def test_pause_is_sent_by_its_virtual_key_alone(captured: list[win32.INPUT]) -> None:
    # Pause's scan code is E1 1D 45 — three bytes that do not fit `wScan`.
    SendInputBackend().key(0x13, down=True)
    [record] = captured
    assert record.u.ki.wScan == 0
    assert record.u.ki.wVk == 0x13
