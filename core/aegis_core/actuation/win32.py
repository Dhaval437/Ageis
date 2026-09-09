"""Raw ctypes bindings for the Win32 input APIs AEGIS needs.

This module is *only* declarations and thin one-line wrappers. Every policy
decision — what to send, what to ignore, when to abort — lives in `input.py`
and `preempt.py`. Keeping the `ctypes` surface in one file means there is
exactly one place to audit for a wrong `argtypes` or a missing signature tag.

Windows-only, by the locked decision in `REMEMBER.md § 4`. Importing it anywhere
else raises immediately rather than failing later at the first API call.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final

if sys.platform != "win32":  # pragma: no cover - AEGIS is Windows-only
    raise ImportError("aegis_core.actuation.win32 requires Windows")

# ---------------------------------------------------------------------------
# Primitive types
# ---------------------------------------------------------------------------

# wintypes has no ULONG_PTR; it is pointer-sized, and `dwExtraInfo` is one.
ULONG_PTR: Final = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
LRESULT: Final = ctypes.c_ssize_t

HOOKPROC: Final = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INPUT_MOUSE: Final = 0
INPUT_KEYBOARD: Final = 1

KEYEVENTF_EXTENDEDKEY: Final = 0x0001
KEYEVENTF_KEYUP: Final = 0x0002
KEYEVENTF_UNICODE: Final = 0x0004

MOUSEEVENTF_MOVE: Final = 0x0001
MOUSEEVENTF_LEFTDOWN: Final = 0x0002
MOUSEEVENTF_LEFTUP: Final = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
MOUSEEVENTF_RIGHTUP: Final = 0x0010
MOUSEEVENTF_MIDDLEDOWN: Final = 0x0020
MOUSEEVENTF_MIDDLEUP: Final = 0x0040
MOUSEEVENTF_WHEEL: Final = 0x0800
MOUSEEVENTF_HWHEEL: Final = 0x1000
MOUSEEVENTF_ABSOLUTE: Final = 0x8000

WHEEL_DELTA: Final = 120

WH_KEYBOARD_LL: Final = 13
WH_MOUSE_LL: Final = 14
HC_ACTION: Final = 0

WM_QUIT: Final = 0x0012
WM_KEYDOWN: Final = 0x0100
WM_KEYUP: Final = 0x0101
WM_SYSKEYDOWN: Final = 0x0104
WM_SYSKEYUP: Final = 0x0105
WM_MOUSEMOVE: Final = 0x0200

# ---------------------------------------------------------------------------
# Structures — field names are Win32's, not ours (see the N815 exemption in
# pyproject.toml). Renaming them would silently break the ABI mapping.
# ---------------------------------------------------------------------------


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


# ---------------------------------------------------------------------------
# Prototypes
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK

user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT

user32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)
user32.GetMessageW.restype = wintypes.BOOL

user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
user32.PostThreadMessageW.restype = wintypes.BOOL

kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# Thin wrappers
# ---------------------------------------------------------------------------


def send_input(events: list[INPUT]) -> None:
    """Inject `events` as one atomic batch.

    Windows can refuse a batch (UIPI, a higher-integrity foreground window, a
    secure desktop). A short write is a real failure and never silent: the
    caller must know how much of a sequence landed so it can release whatever
    it still believes it is holding.
    """
    if not events:
        return
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise ctypes.WinError(ctypes.get_last_error())


def current_thread_id() -> int:
    return int(kernel32.GetCurrentThreadId())


def post_quit(thread_id: int) -> bool:
    """Ask the message loop on `thread_id` to exit. False if the thread is gone."""
    return bool(user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))
