"""The UI Automation tree of one window, flattened into a list of plain elements.

`walk()` reads the foreground window (or any top-level window) through Windows UI
Automation and returns a `UiaTree`: every element in UIA's *control view*, in
document order, with its role, name, value, bounding box and state. This is the
raw material — `P2-04` prunes and ranks it down to the 200 elements a model is
shown, `P2-05` redacts it, and `P2-08` turns an element back into a click point.

How it reads, and why:

* **One cross-process call.** A `CacheRequest` over `TreeScope_Subtree` fetches
  the whole control-view subtree with every property this module needs in a
  single round trip to the target app, and the walk afterwards is in-process.
  Reading property by property is one round trip *each*: measured on an Explorer
  window, 298 elements cached took ~190 ms against ~600 ms for a per-property walk
  over a similar set, and almost all of the cached cost is the target app's own.
* **Bounded on every axis** (`REVIEW.md § 2`). UIA's own connection and
  transaction timeouts stop a hung app from hanging the core; at most
  `MAX_ELEMENTS` elements and `MAX_DEPTH` levels are kept, and every string is cut
  to `MAX_TEXT_CHARS`. A tree that hit a cap says so (`UiaTree.truncated`).
* **One coordinate space.** Bounding boxes are physical virtual-desktop pixels,
  which UIA only reports to a per-monitor-aware thread — so, as for capture,
  `query_layout()` runs first and refuses any other thread, and the layout is
  re-verified afterwards.
* **Password values are never kept.** An element UIA flags `IsPassword` has its
  value dropped here, at the first moment the core holds it. `P2-05` still owns
  redaction proper (labels that *look* secret, and the pixels).

Every string in a `UiaTree` is text from the user's screen: untrusted data, never
an instruction, and none of it is logged — only counts and timings are.

COM threading: `comtypes` initialises COM on the thread that imports it, as a
single-threaded apartment unless told otherwise. UIA clients belong in the
multithreaded apartment, so this module asks for that before `comtypes` loads,
and every thread that walks joins the MTA on first use and stays in it. Nothing
is ever un-initialised: releasing a COM object after its apartment has gone is a
crash, and an exception traceback can keep one alive past any `finally`.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Final, Protocol

from aegis_core.perception import win32
from aegis_core.perception.display import DisplayLayout, Rect, query_layout, verify_layout

#: `COINIT_MULTITHREADED`. Must be in place before `comtypes` is first imported.
COINIT_MULTITHREADED: Final = 0x0
if not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]

import comtypes  # noqa: E402 - needs `sys.coinit_flags` first
import comtypes.client  # noqa: E402

log = logging.getLogger(__name__)

#: At most this many elements are kept. `P2-04` ranks them down to 200; this cap
#: only stops a pathological tree from being held whole. VS Code, a large Electron
#: app, measured 2 619.
MAX_ELEMENTS: Final = 5_000

#: Deeper elements are left out. Real UIs measured here nest under 40 levels.
MAX_DEPTH: Final = 64

#: A name or value longer than this is cut, and ends with `ELLIPSIS`. An edit
#: control's value is its whole text, which can be megabytes.
MAX_TEXT_CHARS: Final = 1_000
ELLIPSIS: Final = "…"

#: UIA's `ConnectionTimeout` and `TransactionTimeout`. Windows' default transaction
#: timeout is 20 s, which is a hung agent from the user's point of view.
TIMEOUT_MS: Final = 2_000

# UIA property ids (`UIAutomationClient.h`). Numeric here so the module loads
# without the generated type library; a test holds them to it.
UIA_RUNTIME_ID: Final = 30000
UIA_BOUNDING_RECTANGLE: Final = 30001
UIA_CONTROL_TYPE: Final = 30003
UIA_NAME: Final = 30005
UIA_HAS_KEYBOARD_FOCUS: Final = 30008
UIA_IS_ENABLED: Final = 30010
UIA_AUTOMATION_ID: Final = 30011
UIA_CLASS_NAME: Final = 30012
UIA_IS_PASSWORD: Final = 30019
UIA_IS_OFFSCREEN: Final = 30022
UIA_VALUE_VALUE: Final = 30045

#: Everything one walk asks the target app for, in one round trip.
PROPERTIES: Final = (
    UIA_RUNTIME_ID,
    UIA_BOUNDING_RECTANGLE,
    UIA_CONTROL_TYPE,
    UIA_NAME,
    UIA_HAS_KEYBOARD_FOCUS,
    UIA_IS_ENABLED,
    UIA_AUTOMATION_ID,
    UIA_CLASS_NAME,
    UIA_IS_PASSWORD,
    UIA_IS_OFFSCREEN,
    UIA_VALUE_VALUE,
)

TREE_SCOPE_SUBTREE: Final = 7
AUTOMATION_ELEMENT_MODE_NONE: Final = 0

#: `UIA_*ControlTypeId`, 50000 upwards, by the names UIA gives them.
ROLES: Final = (
    "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image",
    "ListItem", "List", "Menu", "MenuBar", "MenuItem", "ProgressBar", "RadioButton",
    "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab", "TabItem", "Text", "ToolBar",
    "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid", "DataItem",
    "Document", "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table",
    "TitleBar", "Separator", "SemanticZoom", "AppBar",
)  # fmt: skip
FIRST_CONTROL_TYPE: Final = 50000
UNKNOWN_ROLE: Final = "Unknown"

# HRESULTs worth their own message.
UIA_E_ELEMENTNOTAVAILABLE: Final = 0x80040201
UIA_E_TIMEOUT: Final = 0x80131505
RPC_E_CHANGED_MODE: Final = 0x80010106


class UiaError(RuntimeError):
    """The window's UI tree could not be read, for a reason the message states."""


@dataclass(frozen=True, slots=True)
class UiaElement:
    """One element of a window's control view.

    `id` is its position in this walk — document order, stable only within one
    `UiaTree` — and `parent` the `id` of the element containing it. `runtime_id`
    is UIA's own identity for it, which is what re-finds the element later.
    `name` and `value` are screen text and are kept out of `repr`.
    """

    id: int
    parent: int | None
    depth: int
    role: str
    name: str = field(repr=False)
    #: `None` when the element has no value — or is a password field.
    value: str | None = field(repr=False)
    #: Physical virtual-desktop pixels; `None` when the element has no area.
    bbox: Rect | None
    enabled: bool
    focused: bool
    offscreen: bool
    is_password: bool
    automation_id: str
    class_name: str
    runtime_id: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UiaTree:
    """Every element of one window, at one moment, under one display layout."""

    hwnd: int
    elements: tuple[UiaElement, ...] = field(repr=False)
    #: True if a cap in this module left elements out.
    truncated: bool
    layout: DisplayLayout
    captured_at: float


class UiaNode(Protocol):
    """What the flattening needs from an element: its properties and its children."""

    def prop(self, property_id: int) -> object: ...

    def children(self) -> Sequence[UiaNode]: ...


def walk(hwnd: int | None = None) -> UiaTree:
    """Read the UI tree of `hwnd`, or of the foreground window by default.

    Raises `DpiAwarenessError` on a thread that is not per-monitor aware,
    `LayoutChangedError` if the monitors changed during the walk, and `UiaError`
    when there is no such window, it stopped answering, or UIA refused — each
    with a message a person can act on.
    """
    started = time.perf_counter()
    layout = query_layout()
    target = hwnd if hwnd is not None else win32.foreground_window()
    if not target:
        raise UiaError(
            "No window is in the foreground. This happens while the machine is locked "
            "or a secure desktop, such as a UAC prompt, is showing."
        )
    if not win32.is_window(target):
        raise UiaError("That window no longer exists.")
    captured_at = time.time()
    elements, truncated = _read(target)
    verify_layout(layout)
    log.debug(
        "uia.walked",
        extra={
            "elements": len(elements),
            "truncated": truncated,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return UiaTree(
        hwnd=target,
        elements=elements,
        truncated=truncated,
        layout=layout,
        captured_at=captured_at,
    )


def flatten(
    root: UiaNode, *, max_elements: int = MAX_ELEMENTS, max_depth: int = MAX_DEPTH
) -> tuple[tuple[UiaElement, ...], bool]:
    """`root` and its descendants in document order, and whether a cap cut any out.

    Depth-first, pre-order — the order a sighted reader meets the controls in —
    with an explicit stack, so a deep tree cannot exhaust Python's recursion.
    """
    elements: list[UiaElement] = []
    truncated = False
    stack: list[tuple[UiaNode, int | None, int]] = [(root, None, 0)]
    while stack:
        if len(elements) >= max_elements:
            truncated = True
            break
        node, parent, depth = stack.pop()
        element = _element(node, len(elements), parent, depth)
        elements.append(element)
        children = node.children()
        if not children:
            continue
        if depth >= max_depth:
            truncated = True
            continue
        stack.extend((child, element.id, depth + 1) for child in reversed(children))
    return tuple(elements), truncated


def _element(node: UiaNode, index: int, parent: int | None, depth: int) -> UiaElement:
    """Convert one node, trusting none of UIA's types: providers are other people's code."""
    is_password = node.prop(UIA_IS_PASSWORD) is True
    value = node.prop(UIA_VALUE_VALUE)
    return UiaElement(
        id=index,
        parent=parent,
        depth=depth,
        role=_role(node.prop(UIA_CONTROL_TYPE)),
        name=_text(node.prop(UIA_NAME)) or "",
        value=None if is_password else _text(value),
        bbox=_bbox(node.prop(UIA_BOUNDING_RECTANGLE)),
        enabled=node.prop(UIA_IS_ENABLED) is True,
        focused=node.prop(UIA_HAS_KEYBOARD_FOCUS) is True,
        offscreen=node.prop(UIA_IS_OFFSCREEN) is True,
        is_password=is_password,
        automation_id=_text(node.prop(UIA_AUTOMATION_ID)) or "",
        class_name=_text(node.prop(UIA_CLASS_NAME)) or "",
        runtime_id=_runtime_id(node.prop(UIA_RUNTIME_ID)),
    )


def _role(control_type: object) -> str:
    if isinstance(control_type, int) and not isinstance(control_type, bool):
        offset = control_type - FIRST_CONTROL_TYPE
        if 0 <= offset < len(ROLES):
            return ROLES[offset]
    return UNKNOWN_ROLE


def _text(value: object) -> str | None:
    """A string cut to `MAX_TEXT_CHARS`, or `None` for anything that is not one.

    UIA answers an unsupported property with a sentinel COM object, not an error,
    so "not a string" is the normal way to learn an element has no value.
    """
    if not isinstance(value, str):
        return None
    if len(value) > MAX_TEXT_CHARS:
        return value[: MAX_TEXT_CHARS - len(ELLIPSIS)] + ELLIPSIS
    return value


def _bbox(value: object) -> Rect | None:
    """UIA's `(left, top, width, height)` doubles as a `Rect`, or `None` if it has no area."""
    if not isinstance(value, tuple | list) or len(value) != 4:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        return None
    left, top, width, height = (float(v) for v in value)
    if not all(math.isfinite(v) for v in (left, top, width, height)):
        return None
    rect = Rect(round(left), round(top), round(left + width), round(top + height))
    return None if rect.is_empty else rect


def _runtime_id(value: object) -> tuple[int, ...]:
    if not isinstance(value, tuple | list):
        return ()
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return ()
    return tuple(value)


# --------------------------------------------------------------------------- #
# COM
# --------------------------------------------------------------------------- #

_apartment = threading.local()


def _join_mta() -> None:
    """Put the calling thread in the multithreaded apartment, once. Never left.

    A thread some other code already made single-threaded (`RPC_E_CHANGED_MODE`)
    is used as it is: UIA works there too, only with more marshalling.
    """
    if getattr(_apartment, "joined", False):
        return
    try:
        comtypes.CoInitializeEx(COINIT_MULTITHREADED)
    except OSError as error:
        if (error.winerror or 0) & 0xFFFFFFFF != RPC_E_CHANGED_MODE:
            raise UiaError(f"COM could not be started on this thread ({error}).") from error
    _apartment.joined = True


@cache
def _client_module() -> Any:
    """The `UIAutomationClient` type library, generated by `comtypes` on first use."""
    return comtypes.client.GetModule("UIAutomationCore.dll")


class _ComNode:
    """A `UiaNode` over an element whose properties and children are already cached."""

    __slots__ = ("_element",)

    def __init__(self, element: Any) -> None:
        self._element = element

    def prop(self, property_id: int) -> object:
        value: object = self._element.GetCachedPropertyValue(property_id)
        return value

    def children(self) -> Sequence[UiaNode]:
        array = self._element.GetCachedChildren()
        if not array:
            return ()
        return [_ComNode(array.GetElement(i)) for i in range(array.Length)]


def _read(hwnd: int) -> tuple[tuple[UiaElement, ...], bool]:
    """Fetch `hwnd`'s control view in one round trip and flatten it."""
    _join_mta()
    client = _client_module()
    try:
        automation = comtypes.client.CreateObject(
            client.CUIAutomation8, interface=client.IUIAutomation2
        )
        automation.ConnectionTimeout = TIMEOUT_MS
        automation.TransactionTimeout = TIMEOUT_MS
        request = automation.CreateCacheRequest()
        for property_id in PROPERTIES:
            request.AddProperty(property_id)
        request.TreeScope = TREE_SCOPE_SUBTREE
        request.TreeFilter = automation.ControlViewCondition
        request.AutomationElementMode = AUTOMATION_ELEMENT_MODE_NONE
        root = automation.ElementFromHandleBuildCache(hwnd, request)
        return flatten(_ComNode(root))
    except comtypes.COMError as error:
        raise _uia_error(int(error.hresult)) from None


def _uia_error(hresult: int) -> UiaError:
    code = hresult & 0xFFFFFFFF
    if code == UIA_E_TIMEOUT:
        return UiaError(
            f"That window did not answer within {TIMEOUT_MS // 1000} s. It may be hung."
        )
    if code == UIA_E_ELEMENTNOTAVAILABLE:
        return UiaError("That window closed while it was being read.")
    return UiaError(f"Windows UI Automation could not read that window (0x{code:08X}).")
