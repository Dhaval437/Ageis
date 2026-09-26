"""Shared test doubles and Win32 helpers for the actuation tests."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from aegis_core.actuation.input import MouseButton
from aegis_core.actuation.preempt import PreemptSignal

#: F24. Chosen for the "physical input" injections because no shell, editor, or
#: shortcut in a default Windows install binds it — the tests must not type into
#: whatever window happens to be focused on the developer's machine.
VK_F24 = 0x87


@dataclass
class Recorded:
    """One event the fake backend was asked to send."""

    kind: str
    detail: object
    down: bool | None = None


class RecordingBackend:
    """An `InputBackend` that records instead of touching the desktop.

    `delay` makes a sequence take real time so an abort has something to
    interrupt. `fail_keys` makes a `KEYUP` raise, which is how the release path
    is tested for the "one failure must not strand the other seven" case.
    """

    def __init__(self, *, delay: float = 0.0, fail_keys: frozenset[int] = frozenset()) -> None:
        self.events: list[Recorded] = []
        self.delay = delay
        self.fail_keys = fail_keys
        self._lock = threading.Lock()

    def _record(self, event: Recorded) -> None:
        with self._lock:
            self.events.append(event)
        if self.delay:
            time.sleep(self.delay)

    # -- InputBackend -------------------------------------------------------

    def key(self, vk: int, *, down: bool) -> None:
        if not down and vk in self.fail_keys:
            raise OSError(f"SendInput refused KEYUP for {vk:#04x}")
        self._record(Recorded("key", vk, down))

    def unicode_char(self, char: str) -> None:
        self._record(Recorded("char", char))

    def mouse_move(self, dx: int, dy: int) -> None:
        self._record(Recorded("move", (dx, dy)))

    def mouse_move_absolute(self, ax: int, ay: int) -> None:
        self._record(Recorded("move_to", (ax, ay)))

    def mouse_button(self, button: MouseButton, *, down: bool) -> None:
        self._record(Recorded("button", button, down))

    def scroll(self, dx: int, dy: int) -> None:
        self._record(Recorded("scroll", (dx, dy)))

    # -- assertions ---------------------------------------------------------

    def key_ups(self) -> list[int]:
        with self._lock:
            return [e.detail for e in self.events if e.kind == "key" and e.down is False]  # type: ignore[misc]

    def key_downs(self) -> list[int]:
        with self._lock:
            return [e.detail for e in self.events if e.kind == "key" and e.down is True]  # type: ignore[misc]

    def kinds(self) -> list[str]:
        with self._lock:
            return [e.kind for e in self.events]


# ---------------------------------------------------------------------------
# Real Win32 injection — used only by the hook tests
# ---------------------------------------------------------------------------


def inject_untagged_key(vk: int = VK_F24) -> None:
    """Send a keystroke with `dwExtraInfo = 0` — indistinguishable from a real one.

    A low-level hook cannot tell this from a keypress on the physical keyboard:
    `LLKHF_INJECTED` is deliberately *not* what AEGIS keys off (see
    `signature.py`), so from the hook's point of view this is the human. That is
    what makes it a valid stand-in for a finger on a key in an automated test.
    """
    if sys.platform != "win32":  # pragma: no cover - guarded by the module skip
        raise RuntimeError("Windows only")
    from aegis_core.actuation import win32

    events = []
    for up in (False, True):
        record = win32.INPUT(type=win32.INPUT_KEYBOARD)
        record.u.ki = win32.KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=win32.KEYEVENTF_KEYUP if up else 0,
            time=0,
            dwExtraInfo=0,
        )
        events.append(record)
    win32.send_input(events)


#: Integrity-level RIDs (`winnt.h`). UIPI compares these.
MEDIUM_INTEGRITY = 0x2000
HIGH_INTEGRITY = 0x3000


def process_integrity(pid: int) -> int | None:
    """The integrity RID of `pid`'s token, or `None` if we may not read it."""
    if sys.platform != "win32":  # pragma: no cover - guarded by the module skip
        raise RuntimeError("Windows only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    process_query_limited_information, token_query, token_integrity_level = 0x1000, 0x0008, 25
    process = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, token_query, ctypes.byref(token)):
            return None
        try:
            buffer = ctypes.create_string_buffer(64)
            size = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token, token_integrity_level, buffer, len(buffer), ctypes.byref(size)
            ):
                return None
            # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES, whose first field is the SID.
            sid = ctypes.c_void_p.from_buffer(buffer).value
            count = advapi32.GetSidSubAuthorityCount(sid).contents.value
            return int(advapi32.GetSidSubAuthority(sid, count - 1).contents.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def foreground_outranks_us() -> str | None:
    """Describe the focused window if UIPI walls it off from us, else `None`.

    While a window of a higher integrity level than ours has focus (Task Manager,
    an elevated terminal, a UAC prompt), Windows drops the keystrokes we inject and
    does not call our low-level hook for them: measured, 0 of 5 reached the hook
    with Task Manager in front, 5 of 5 without. A process whose token we may not
    read at all is treated the same, since that is itself a sign it outranks us.
    """
    if sys.platform != "win32":  # pragma: no cover - guarded by the module skip
        raise RuntimeError("Windows only")
    import ctypes
    import os
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

    window = user32.GetForegroundWindow()
    if not window:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    ours, theirs = process_integrity(os.getpid()), process_integrity(pid.value)
    if ours is not None and theirs is not None and theirs <= ours:
        return None
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(window, title, len(title))
    level = "unreadable" if theirs is None else f"{theirs:#06x}"
    return f"{title.value!r} (pid {pid.value}, integrity {level}; ours {ours:#06x})"


#: How long a missed keystroke may still turn up before we call it lost rather than late.
LATE_S = 2.0


class _Monitor(Protocol):
    @property
    def running(self) -> bool: ...


def explain_miss(signal: PreemptSignal, monitor: _Monitor, sent_at: float) -> tuple[str, bool]:
    """Why an injected, untagged keystroke did not preempt. Call it straight after the miss.

    Returns the cause and whether it lies outside AEGIS (an environment the test
    cannot run in, rather than a fault). The checks go from cheapest to most
    intrusive, and each rules out the one before:

    1. a higher-integrity window has focus — UIPI, the one cause measured to do it;
    2. the key arrives late — the hook thread was starved;
    3. the hook thread is dead;
    4. a second key gets through — the first was eaten by a hook ahead of ours;
    5. the second is lost too — the hook no longer fires (Windows unhooks a
       callback that overruns `LowLevelHooksTimeout`, and says nothing).
    """
    blocker = foreground_outranks_us()
    if blocker is not None:
        return (
            f"a window of higher integrity has focus, so UIPI withholds our input: {blocker}",
            True,
        )
    if signal.wait(LATE_S):
        last = signal.last
        late_ms = (last.at - sent_at) * 1000 if last is not None else float("nan")
        return f"the key arrived {late_ms:.0f} ms late: the hook thread was starved", False
    if not monitor.running:
        return "the hook thread is dead", False
    inject_untagged_key()
    if signal.wait(DELIVERY_RETRY_S):
        return (
            "one keystroke was eaten: a second got through, "
            "so a hook ahead of ours swallowed the first",
            False,
        )
    return (
        "the hook no longer fires though its thread is alive: Windows removed it "
        "(LowLevelHooksTimeout) or input is blocked",
        False,
    )


#: How long `explain_miss` gives its second keystroke.
DELIVERY_RETRY_S = 0.25


def skip_if_walled_off() -> None:
    """Skip a live test up front when UIPI would withhold every key it sends."""
    import pytest

    blocker = foreground_outranks_us()
    if blocker is not None:
        pytest.skip(
            f"a window of higher integrity has focus, so UIPI withholds our input: {blocker}"
        )


def expect_preempted(
    signal: PreemptSignal, monitor: _Monitor, sent_at: float, wait_s: float
) -> None:
    """Assert the key sent at `sent_at` preempted; on a miss, say why, and skip if not ours."""
    import pytest

    if signal.wait(wait_s):
        return
    cause, environmental = explain_miss(signal, monitor, sent_at)
    if environmental:
        pytest.skip(f"untagged input could not be tested: {cause}")
    pytest.fail(f"untagged input did not preempt within {wait_s * 1000:.0f} ms: {cause}")


def wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.001
) -> bool:
    """Poll `predicate` until true or `timeout` elapses."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def settle(signal: PreemptSignal, quiet_for: float = 0.15, timeout: float = 5.0) -> bool:
    """Wait for a stretch with no preemption at all, then re-arm the signal.

    The hook watches the *whole machine*, so a developer nudging the mouse while
    the suite runs looks exactly like the thing under test. Rather than sleep and
    hope, wait for a genuinely idle window; the caller skips loudly if there
    never is one.
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        signal.clear()
        if not signal.wait(quiet_for):
            return True
    return False
