"""Raw ctypes bindings for the Win32 display and DPI APIs perception needs.

This module is *only* declarations and thin wrappers, for the same reason as
`aegis_core.actuation.win32`: one file to audit for a wrong `argtypes`. Every
decision — what counts as a usable layout, when to refuse — is in `display.py`.

The two modules keep separate `WinDLL` objects on purpose. `argtypes` is set on
the function pointer a `WinDLL` instance hands out, so sharing one would let a
declaration here silently change a prototype the input layer relies on.

Windows-only, by the locked decision in `REMEMBER.md § 4`.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final

if sys.platform != "win32":  # pragma: no cover - AEGIS is Windows-only
    raise ImportError("aegis_core.perception.win32 requires Windows")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: `DPI_AWARENESS_CONTEXT` pseudo-handles (`windef.h`). They are small negative
#: numbers cast to `HANDLE`, not real handles.
DPI_AWARENESS_CONTEXT_UNAWARE: Final = -1
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final = -4

#: `DPI_AWARENESS` values returned by `GetAwarenessFromDpiAwarenessContext`.
DPI_AWARENESS_INVALID: Final = -1
DPI_AWARENESS_UNAWARE: Final = 0
DPI_AWARENESS_SYSTEM_AWARE: Final = 1
DPI_AWARENESS_PER_MONITOR_AWARE: Final = 2

ERROR_ACCESS_DENIED: Final = 5

MDT_EFFECTIVE_DPI: Final = 0
MONITORINFOF_PRIMARY: Final = 0x1
CCHDEVICENAME: Final = 32

SM_XVIRTUALSCREEN: Final = 76
SM_YVIRTUALSCREEN: Final = 77
SM_CXVIRTUALSCREEN: Final = 78
SM_CYVIRTUALSCREEN: Final = 79

# ---------------------------------------------------------------------------
# Structures — field names are Win32's (see the N815 exemption in pyproject.toml)
# ---------------------------------------------------------------------------


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * CCHDEVICENAME),
    )


MONITORENUMPROC: Final = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

# ---------------------------------------------------------------------------
# Prototypes
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")

user32.SetProcessDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL

user32.GetThreadDpiAwarenessContext.argtypes = ()
user32.GetThreadDpiAwarenessContext.restype = wintypes.HANDLE

user32.SetThreadDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE

user32.GetAwarenessFromDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

user32.EnumDisplayMonitors.argtypes = (
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    MONITORENUMPROC,
    wintypes.LPARAM,
)
user32.EnumDisplayMonitors.restype = wintypes.BOOL

user32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
user32.GetMonitorInfoW.restype = wintypes.BOOL

user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int

# HRESULT is declared as a plain LONG rather than `ctypes.HRESULT`, which would
# raise its own OSError before `display.py` could say which monitor failed.
shcore.GetDpiForMonitor.argtypes = (
    wintypes.HMONITOR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
)
shcore.GetDpiForMonitor.restype = wintypes.LONG

# ---------------------------------------------------------------------------
# Thin wrappers
# ---------------------------------------------------------------------------


def set_process_dpi_awareness_context(context: int) -> tuple[bool, int]:
    """Set the process default. Returns `(succeeded, GetLastError())`.

    `ERROR_ACCESS_DENIED` means the process default was already set — by an
    earlier call, a manifest, or a library — and cannot be changed again.
    """
    ok = bool(user32.SetProcessDpiAwarenessContext(context))
    return ok, 0 if ok else ctypes.get_last_error()


def thread_dpi_awareness() -> int:
    """The calling thread's `DPI_AWARENESS`. Coordinates are per-thread in Windows."""
    return int(user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext()))


def set_thread_dpi_awareness_context(context: int) -> int | None:
    """Override the calling thread only. Returns the previous context, `None` on failure."""
    previous: int | None = user32.SetThreadDpiAwarenessContext(context)
    return previous


def enum_display_monitors(limit: int) -> list[int]:
    """Every `HMONITOR` on the desktop, stopping once more than `limit` are seen.

    The callback does nothing but append: an exception raised inside a ctypes
    callback is printed and swallowed, so there must be nothing in it to raise.
    Returning `limit + 1` handles lets the caller tell "exactly `limit`" from
    "too many" without the list growing unbounded.
    """
    handles: list[int] = []

    def collect(hmonitor: int, _hdc: int, _rect: object, _data: int) -> bool:
        handles.append(hmonitor)
        return len(handles) <= limit

    callback = MONITORENUMPROC(collect)
    if not user32.EnumDisplayMonitors(None, None, callback, 0) and len(handles) <= limit:
        raise ctypes.WinError(ctypes.get_last_error())
    return handles


def monitor_info(hmonitor: int) -> MONITORINFOEXW | None:
    """`GetMonitorInfoW`, or `None` if the monitor has gone since it was enumerated."""
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return None
    return info


def monitor_dpi(hmonitor: int) -> int | None:
    """The monitor's effective DPI, or `None` if Windows would not say."""
    dpi_x = wintypes.UINT()
    dpi_y = wintypes.UINT()
    result = shcore.GetDpiForMonitor(
        hmonitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
    )
    if result != 0:
        return None
    # The docs guarantee x == y for the effective DPI; x is the one to trust.
    return int(dpi_x.value)


def virtual_screen() -> tuple[int, int, int, int]:
    """`(left, top, width, height)` of the virtual desktop, as the calling thread sees it."""
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )
