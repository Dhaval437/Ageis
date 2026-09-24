"""Plain Win32 windows for the live perception tests to aim at.

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
import queue
import threading
import time
from collections.abc import Callable
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


# --------------------------------------------------------------------------- #
# A window with real controls in it, for the UI Automation tests
# --------------------------------------------------------------------------- #

WS_CHILD: Final = 0x40000000
WS_VISIBLE: Final = 0x10000000
WS_DISABLED: Final = 0x08000000
ES_PASSWORD: Final = 0x0020
ES_AUTOHSCROLL: Final = 0x0080
#: Without it a static control is transparent to hit testing and a click passes through.
SS_NOTIFY: Final = 0x0100
COLOR_BTNFACE: Final = 15

FORM_CLASS_NAME: Final = "AegisUiaTestWindow"
_form_class_registered = False

user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
user32.SetWindowTextW.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT,
)  # fmt: skip
user32.SetWindowPos.restype = wintypes.BOOL

HWND_TOP: Final = 0
SWP_NOSIZE: Final = 0x0001
SWP_NOMOVE: Final = 0x0002
SWP_NOZORDER: Final = 0x0004
SWP_NOACTIVATE: Final = 0x0010


def _register_form_class() -> None:
    global _form_class_registered
    if _form_class_registered:
        return
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _window_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hbrBackground = COLOR_BTNFACE + 1  # a system colour brush, never freed
    wc.lpszClassName = FORM_CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(error)
    _form_class_registered = True


class FormWindow:
    """A topmost, never-activated window holding a button, a text box and a password box.

    It lives on its **own thread**, which pumps its messages: UI Automation calls
    into a window's thread, so a window owned by the thread doing the walking
    would deadlock it. `hang()` stops the pumping, which is how a hung app looks
    from outside. Use as a context manager.
    """

    TITLE: Final = "AEGIS form test"
    BUTTON: Final = "Save changes"
    DISABLED_BUTTON: Final = "Unavailable"
    TEXT: Final = "visible text"
    #: What is typed into the password box. It must never appear in a tree.
    SECRET: Final = "hunter2-correct-horse"  # noqa: S105 - a test fixture, not a credential

    def __init__(self, rect: Rect) -> None:
        self.rect = rect
        self.hwnd = 0
        self.children: dict[str, int] = {}
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._hung = threading.Event()
        self._calls: queue.Queue[tuple[Callable[[], None], threading.Event]] = queue.Queue()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="uia-form-window", daemon=True)

    def __enter__(self) -> FormWindow:
        self._thread.start()
        if not self._ready.wait(5):
            raise TimeoutError("the form window was never created")
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *_exc: object) -> None:
        self._hung.clear()
        self._stop.set()
        self._thread.join(10)

    def hang(self, seconds: float) -> None:
        """Stop answering messages for `seconds`."""
        self._hang_for = seconds
        self._hung.set()

    def move_to(self, left: int, top: int) -> None:
        """Move the window, keeping its size; its controls move with it."""

        def move() -> None:
            flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            if not user32.SetWindowPos(self.hwnd, None, left, top, 0, 0, flags):
                raise ctypes.WinError(ctypes.get_last_error())

        self._on_window_thread(move)
        self.rect = Rect(left, top, left + self.rect.width, top + self.rect.height)

    def cover(self, key: str, *, takes_clicks: bool = True) -> None:
        """Put a static label on top of control `key`, inside this same window.

        Something drawn over a control by its own app — no other window is
        involved, so only UI Automation's hit test can see it. With
        `takes_clicks`, the label is `SS_NOTIFY` and catches the clicks aimed at
        the control; without, it is click-through, as most labels are.
        """

        def add() -> None:
            rect = self.child_rect(key)
            style = SS_NOTIFY if takes_clicks else 0
            x, y = rect.left - self.rect.left, rect.top - self.rect.top
            cover = self._create(self.hwnd, "STATIC", "cover", style, x, y)
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            if not user32.SetWindowPos(cover, HWND_TOP, 0, 0, 0, 0, flags):
                raise ctypes.WinError(ctypes.get_last_error())
            self.children["cover"] = cover

        self._on_window_thread(add)

    def _on_window_thread(self, call: Callable[[], None]) -> None:
        """Run `call` on the thread that owns the window, and wait for it."""
        done = threading.Event()
        self._calls.put((call, done))
        if not done.wait(5):
            raise TimeoutError("the form window's thread did not run the call")

    def child_rect(self, key: str) -> Rect:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(self.children[key], ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Rect(rect.left, rect.top, rect.right, rect.bottom)

    def _create(self, parent: int, cls: str, text: str, style: int, x: int, y: int) -> int:
        hwnd: int | None = user32.CreateWindowExW(
            0, cls, text, WS_CHILD | WS_VISIBLE | style, x, y, 240, 40,
            parent, None, kernel32.GetModuleHandleW(None), None,
        )  # fmt: skip
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def _run(self) -> None:
        try:
            _register_form_class()
            rect = self.rect
            hwnd: int | None = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                FORM_CLASS_NAME, self.TITLE, WS_POPUP,
                rect.left, rect.top, rect.width, rect.height,
                None, None, kernel32.GetModuleHandleW(None), None,
            )  # fmt: skip
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = hwnd
            self.children = {
                "button": self._create(hwnd, "BUTTON", self.BUTTON, 0, 20, 20),
                "disabled": self._create(hwnd, "BUTTON", self.DISABLED_BUTTON, WS_DISABLED, 20, 70),
                "text": self._create(hwnd, "EDIT", self.TEXT, ES_AUTOHSCROLL, 20, 120),
                "password": self._create(hwnd, "EDIT", "", ES_PASSWORD | ES_AUTOHSCROLL, 20, 170),
            }
            user32.SetWindowTextW(self.children["password"], self.SECRET)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        except BaseException as error:  # handed to the thread that asked for the window
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                if self._hung.is_set():
                    self._hung.clear()
                    time.sleep(self._hang_for)
                while not self._calls.empty():
                    call, done = self._calls.get()
                    try:
                        call()
                    finally:
                        done.set()
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                time.sleep(0.005)
        finally:
            user32.DestroyWindow(hwnd)


# --------------------------------------------------------------------------- #
# A window that draws text UI Automation knows nothing about, for the OCR tests
# --------------------------------------------------------------------------- #

WM_PAINT: Final = 0x000F
COLOR_WINDOW: Final = 5
FW_NORMAL: Final = 400
DEFAULT_CHARSET: Final = 1
ANTIALIASED_QUALITY: Final = 4
TRANSPARENT: Final = 1
DT_LEFT: Final = 0x0000
DT_SINGLELINE: Final = 0x0020
DT_NOPREFIX: Final = 0x0800

CANVAS_CLASS_NAME: Final = "AegisOcrTestWindow"


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = (
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    )


user32.BeginPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.EndPaint.restype = wintypes.BOOL
user32.DrawTextW.argtypes = (
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT
)  # fmt: skip
user32.DrawTextW.restype = ctypes.c_int
user32.InvalidateRect.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
user32.InvalidateRect.restype = wintypes.BOOL
gdi32.CreateFontW.argtypes = (
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
)  # fmt: skip
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
gdi32.SetBkMode.restype = ctypes.c_int

#: What each live canvas draws, by window handle. Read on the window's own thread.
_canvas_lines: dict[int, tuple[str, ...]] = {}
_canvas_class_registered = False


def _paint(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    if message != WM_PAINT or hwnd not in _canvas_lines:
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))
    paint = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
    font = gdi32.CreateFontW(
        -CanvasWindow.TEXT_PX, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
        0, 0, ANTIALIASED_QUALITY, 0, "Segoe UI",
    )  # fmt: skip
    previous = gdi32.SelectObject(hdc, font)
    gdi32.SetBkMode(hdc, TRANSPARENT)
    for index, line in enumerate(_canvas_lines[hwnd]):
        top = CanvasWindow.MARGIN_PX + index * CanvasWindow.LINE_PX
        rect = wintypes.RECT(CanvasWindow.MARGIN_PX, top, 4000, top + CanvasWindow.LINE_PX)
        user32.DrawTextW(hdc, line, -1, ctypes.byref(rect), DT_LEFT | DT_SINGLELINE | DT_NOPREFIX)
    gdi32.SelectObject(hdc, previous)
    gdi32.DeleteObject(font)
    user32.EndPaint(hwnd, ctypes.byref(paint))
    return 0


#: Kept alive for the process: the class holds a pointer to it.
_paint_proc = WNDPROC(_paint)


def _register_canvas_class() -> None:
    global _canvas_class_registered
    if _canvas_class_registered:
        return
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _paint_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hbrBackground = COLOR_WINDOW + 1  # a system colour brush, never freed
    wc.lpszClassName = CANVAS_CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(error)
    _canvas_class_registered = True


class CanvasWindow:
    """A topmost, never-activated window that paints lines of text itself.

    Nothing in it is a control, so UI Automation sees one empty pane: the case a
    canvas, a game or an app with accessibility off presents. Owned by its own
    thread, like `FormWindow`. Use as a context manager.
    """

    TITLE: Final = "AEGIS canvas test"
    #: Physical pixels. Large enough for OCR to read at any scaling.
    TEXT_PX: Final = 40
    LINE_PX: Final = 64
    MARGIN_PX: Final = 24

    def __init__(self, rect: Rect, lines: tuple[str, ...]) -> None:
        self.rect = rect
        self.lines = lines
        self.hwnd = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="ocr-canvas-window", daemon=True)

    def __enter__(self) -> CanvasWindow:
        self._thread.start()
        if not self._ready.wait(5):
            raise TimeoutError("the canvas window was never created")
        if self._error is not None:
            raise self._error
        time.sleep(0.2)  # let it paint
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(10)

    def set_lines(self, lines: tuple[str, ...]) -> None:
        """Paint `lines` instead, as an app redrawing its own content would."""
        self.lines = lines
        _canvas_lines[self.hwnd] = lines
        if not user32.InvalidateRect(self.hwnd, None, True):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.2)  # let it paint

    def line_rect(self, index: int) -> Rect:
        """The band line `index` is drawn in, in physical pixels."""
        top = self.rect.top + self.MARGIN_PX + index * self.LINE_PX
        return Rect(self.rect.left, top, self.rect.right, top + self.LINE_PX)

    def _run(self) -> None:
        try:
            _register_canvas_class()
            rect = self.rect
            hwnd: int | None = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                CANVAS_CLASS_NAME, self.TITLE, WS_POPUP,
                rect.left, rect.top, rect.width, rect.height,
                None, None, kernel32.GetModuleHandleW(None), None,
            )  # fmt: skip
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = hwnd
            _canvas_lines[hwnd] = self.lines
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        except BaseException as error:  # handed to the thread that asked for the window
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                time.sleep(0.005)
        finally:
            _canvas_lines.pop(hwnd, None)
            user32.DestroyWindow(hwnd)
