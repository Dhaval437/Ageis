"""A plain Win32 window for the live capture tests to aim at.

Tk was the first choice and was flaky here: creating several Tk roots in one
process intermittently failed to load `init.tcl`. A window class whose procedure
is `DefWindowProcW` and whose background brush is the colour under test needs
no toolkit, paints itself on `WM_ERASEBKGND`, and can be minimised, hidden and
destroyed with one call each.

Its own `WinDLL` objects, for the reason `perception/win32.py` gives: `argtypes`
set here must not change a prototype production code relies on.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Final

from aegis_core.perception.display import Rect

WNDPROC: Final = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WS_POPUP: Final = 0x80000000
WS_EX_TOPMOST: Final = 0x00000008
WS_EX_TOOLWINDOW: Final = 0x00000080
WS_EX_NOACTIVATE: Final = 0x08000000
SW_HIDE: Final = 0
SW_SHOWNOACTIVATE: Final = 4
SW_MINIMIZE: Final = 6
PM_REMOVE: Final = 0x0001
ERROR_CLASS_ALREADY_EXISTS: Final = 1410

CLASS_NAME: Final = "AegisCaptureTestWindow"


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    )


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.DestroyWindow.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
)
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t

_class_registered = False
#: Kept alive for the process: the class holds a pointer to it.
_window_proc = ctypes.cast(user32.DefWindowProcW, WNDPROC)


def _register_class(rgb: tuple[int, int, int]) -> None:
    """Register the window class once per process; its brush lives as long as it does."""
    global _class_registered
    if _class_registered:
        return
    red, green, blue = rgb
    brush = gdi32.CreateSolidBrush(red | (green << 8) | (blue << 16))
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _window_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hbrBackground = brush
    wc.lpszClassName = CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(error)
    _class_registered = True


class SolidWindow:
    """A topmost, borderless, never-activated window of one colour.

    Borderless, so its drawn bounds are exactly the rectangle it was created with;
    never activated, so it cannot take the developer's keyboard focus. Create,
    use and destroy it on one thread: that thread owns its message queue.
    """

    #: The class brush is fixed at registration, so every window is this colour.
    RGB: Final = (255, 0, 0)

    def __init__(self, rect: Rect) -> None:
        _register_class(self.RGB)
        self.rect = rect
        hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            CLASS_NAME,
            "AEGIS capture test",
            WS_POPUP,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd: int = hwnd
        self.show()

    def show(self) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self.pump()

    def minimise(self) -> None:
        user32.ShowWindow(self.hwnd, SW_MINIMIZE)
        self.pump()

    def hide(self) -> None:
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self.pump()

    def destroy(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.pump()

    def pump(self, seconds: float = 0.05) -> None:
        """Dispatch this thread's messages for `seconds`, so the window paints and moves."""
        message = wintypes.MSG()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.005)
