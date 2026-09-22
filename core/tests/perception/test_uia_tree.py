"""Tests for `perception/uia_tree.py` — the UI Automation walk.

Three layers, like `test_screen.py`:

* pure — flattening and every property conversion, on hand-built nodes that
  answer the way real providers do, including badly;
* refusals — each reason `walk()` gives up, with the Win32 and COM calls replaced;
* live — a real window this test opens, holding a button, a disabled button, a
  text box and a password box, walked through the real UI Automation stack.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator, Sequence

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows UI Automation")

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402
from aegis_core.perception import display, uia_tree  # noqa: E402
from aegis_core.perception import win32 as display_win32  # noqa: E402
from aegis_core.perception.display import (  # noqa: E402
    DisplayLayout,
    DpiAwarenessError,
    LayoutChangedError,
    Monitor,
    Rect,
    ensure_dpi_awareness,
    query_layout,
)
from aegis_core.perception.uia_tree import (  # noqa: E402
    ELLIPSIS,
    MAX_TEXT_CHARS,
    PROPERTIES,
    ROLES,
    TIMEOUT_MS,
    UNKNOWN_ROLE,
    UiaElement,
    UiaError,
    UiaNode,
    UiaTree,
    flatten,
    walk,
)

from tests.perception.helpers import FormWindow  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _per_monitor_aware() -> None:
    ensure_dpi_awareness()


# --------------------------------------------------------------------------- #
# Hand-built nodes
# --------------------------------------------------------------------------- #

#: What UIA hands back for a property the provider does not implement, when
#: asked without a default. Not a string, not a number.
NOT_SUPPORTED = object()


class FakeNode:
    """A `UiaNode` answering from a dict, with UIA's defaults for anything unset."""

    def __init__(self, name: str = "", *children: FakeNode, **props: object) -> None:
        self._children = children
        self._props: dict[int, object] = {
            uia_tree.UIA_CONTROL_TYPE: 50000,  # Button
            uia_tree.UIA_NAME: name,
            uia_tree.UIA_BOUNDING_RECTANGLE: (10.0, 20.0, 100.0, 30.0),
            uia_tree.UIA_IS_ENABLED: True,
            uia_tree.UIA_HAS_KEYBOARD_FOCUS: False,
            uia_tree.UIA_IS_OFFSCREEN: False,
            uia_tree.UIA_IS_PASSWORD: False,
            uia_tree.UIA_IS_VALUE_PATTERN_AVAILABLE: False,
            # A cached property the element does not support reads as its default.
            uia_tree.UIA_VALUE_VALUE: "",
            uia_tree.UIA_AUTOMATION_ID: "",
            uia_tree.UIA_CLASS_NAME: "",
            uia_tree.UIA_RUNTIME_ID: (42, 1),
        }
        for key, value in props.items():
            self._props[getattr(uia_tree, key)] = value

    def prop(self, property_id: int) -> object:
        return self._props[property_id]

    def children(self) -> Sequence[UiaNode]:
        return self._children


def one(node: FakeNode) -> UiaElement:
    elements, truncated = flatten(node)
    assert not truncated
    assert len(elements) == 1
    return elements[0]


def chain(length: int) -> FakeNode:
    """A tree `length` nodes deep with one child at each level, built without recursion."""
    node = FakeNode(f"level {length - 1}")
    for level in range(length - 2, -1, -1):
        node = FakeNode(f"level {level}", node)
    return node


# --------------------------------------------------------------------------- #
# Pure: flattening
# --------------------------------------------------------------------------- #


def test_elements_come_out_in_document_order_with_their_parents_and_depths() -> None:
    root = FakeNode(
        "window",
        FakeNode("toolbar", FakeNode("open"), FakeNode("save")),
        FakeNode("editor"),
    )
    elements, truncated = flatten(root)
    assert not truncated
    assert [e.name for e in elements] == ["window", "toolbar", "open", "save", "editor"]
    assert [e.id for e in elements] == [0, 1, 2, 3, 4]
    assert [e.parent for e in elements] == [None, 0, 1, 1, 0]
    assert [e.depth for e in elements] == [0, 1, 2, 2, 1]


def test_a_tree_at_exactly_the_element_cap_is_not_truncated() -> None:
    root = FakeNode("root", *(FakeNode(str(i)) for i in range(9)))
    elements, truncated = flatten(root, max_elements=10)
    assert len(elements) == 10
    assert not truncated


def test_the_element_cap_keeps_the_first_elements_and_says_so() -> None:
    root = FakeNode("root", *(FakeNode(str(i)) for i in range(20)))
    elements, truncated = flatten(root, max_elements=5)
    assert truncated
    assert [e.name for e in elements] == ["root", "0", "1", "2", "3"]


def test_the_depth_cap_leaves_deeper_elements_out_and_says_so() -> None:
    elements, truncated = flatten(chain(10), max_depth=3)
    assert truncated
    assert [e.depth for e in elements] == [0, 1, 2, 3]


def test_a_leaf_at_the_depth_cap_is_not_truncation() -> None:
    elements, truncated = flatten(chain(4), max_depth=3)
    assert not truncated
    assert len(elements) == 4


def test_a_very_deep_tree_does_not_exhaust_the_recursion_limit() -> None:
    depth = sys.getrecursionlimit() * 3
    elements, truncated = flatten(chain(depth), max_elements=depth, max_depth=depth)
    assert not truncated
    assert len(elements) == depth
    assert elements[-1].depth == depth - 1


# --------------------------------------------------------------------------- #
# Pure: one element
# --------------------------------------------------------------------------- #


def test_an_element_with_no_value_pattern_has_no_value() -> None:
    """UIA reads an unsupported value as `""`; a button must not look like an empty box."""
    assert one(FakeNode("OK")).value is None


def test_an_empty_text_box_has_an_empty_value() -> None:
    element = one(FakeNode(UIA_IS_VALUE_PATTERN_AVAILABLE=True, UIA_VALUE_VALUE=""))
    assert element.value == ""


def test_a_value_is_kept_when_the_element_has_one() -> None:
    element = one(FakeNode(UIA_IS_VALUE_PATTERN_AVAILABLE=True, UIA_VALUE_VALUE="42"))
    assert element.value == "42"


def test_a_password_fields_value_is_never_kept() -> None:
    element = one(
        FakeNode(
            UIA_IS_PASSWORD=True,
            UIA_IS_VALUE_PATTERN_AVAILABLE=True,
            UIA_VALUE_VALUE=FormWindow.SECRET,
        )
    )
    assert element.is_password
    assert element.value is None
    assert FormWindow.SECRET not in repr(element)


def test_name_and_value_stay_out_of_repr() -> None:
    element = one(
        FakeNode("screen text", UIA_IS_VALUE_PATTERN_AVAILABLE=True, UIA_VALUE_VALUE="more text")
    )
    assert "screen text" not in repr(element)
    assert "more text" not in repr(element)


def test_long_text_is_cut_to_the_cap_and_marked() -> None:
    element = one(
        FakeNode(
            "n" * (MAX_TEXT_CHARS + 50),
            UIA_IS_VALUE_PATTERN_AVAILABLE=True,
            UIA_VALUE_VALUE="v" * 1_000_000,
        )
    )
    assert len(element.name) == MAX_TEXT_CHARS
    assert element.name.endswith(ELLIPSIS)
    assert element.value is not None
    assert len(element.value) == MAX_TEXT_CHARS
    assert element.value.endswith(ELLIPSIS)


def test_text_exactly_at_the_cap_is_left_whole() -> None:
    assert one(FakeNode("n" * MAX_TEXT_CHARS)).name == "n" * MAX_TEXT_CHARS


@pytest.mark.parametrize("junk", [NOT_SUPPORTED, None, 7, b"bytes"])
def test_a_name_that_is_not_a_string_reads_as_empty(junk: object) -> None:
    element = one(FakeNode(UIA_NAME=junk, UIA_AUTOMATION_ID=junk, UIA_CLASS_NAME=junk))
    assert (element.name, element.automation_id, element.class_name) == ("", "", "")


@pytest.mark.parametrize(
    ("control_type", "role"),
    [(50000, "Button"), (50004, "Edit"), (50032, "Window"), (50040, "AppBar")],
)
def test_control_types_map_to_uia_names(control_type: int, role: str) -> None:
    assert one(FakeNode(UIA_CONTROL_TYPE=control_type)).role == role


@pytest.mark.parametrize("junk", [49999, 50000 + len(ROLES), True, "50000", NOT_SUPPORTED])
def test_an_unknown_control_type_is_unknown_not_a_guess(junk: object) -> None:
    assert one(FakeNode(UIA_CONTROL_TYPE=junk)).role == UNKNOWN_ROLE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((10.0, 20.0, 100.0, 30.0), Rect(10, 20, 110, 50)),
        ([10, 20, 100, 30], Rect(10, 20, 110, 50)),
        # A second monitor left of the primary has negative coordinates.
        ((-1280.0, 56.0, 300.0, 40.0), Rect(-1280, 56, -980, 96)),
        ((10.4, 20.6, 99.8, 30.0), Rect(10, 21, 110, 51)),
    ],
)
def test_a_bounding_rectangle_becomes_a_rect(raw: object, expected: Rect) -> None:
    assert one(FakeNode(UIA_BOUNDING_RECTANGLE=raw)).bbox == expected


@pytest.mark.parametrize(
    "raw",
    [
        (0.0, 0.0, 0.0, 0.0),  # what UIA reports for an element with no area
        (10.0, 20.0, 0.0, 30.0),
        (10.0, 20.0, 100.0, -5.0),
        (float("nan"), 0.0, 10.0, 10.0),
        (0.0, 0.0, float("inf"), 10.0),
        (True, 0, 10, 10),
        (1.0, 2.0, 3.0),
        "10,20,100,30",
        NOT_SUPPORTED,
    ],
)
def test_a_bounding_rectangle_with_no_area_or_nonsense_is_none(raw: object) -> None:
    assert one(FakeNode(UIA_BOUNDING_RECTANGLE=raw)).bbox is None


def test_flags_must_be_true_not_merely_truthy() -> None:
    element = one(
        FakeNode(
            UIA_IS_ENABLED=1,
            UIA_HAS_KEYBOARD_FOCUS="yes",
            UIA_IS_OFFSCREEN=NOT_SUPPORTED,
            UIA_IS_PASSWORD=1,
        )
    )
    assert (element.enabled, element.focused, element.offscreen, element.is_password) == (
        False,
        False,
        False,
        False,
    )


def test_a_value_is_kept_only_when_the_value_pattern_is_reported_true() -> None:
    element = one(FakeNode(UIA_IS_VALUE_PATTERN_AVAILABLE=1, UIA_VALUE_VALUE="42"))
    assert element.value is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [((42, 7, 3), (42, 7, 3)), ([1, 2], (1, 2)), ((1, "2"), ()), ((True,), ()), (None, ())],
)
def test_runtime_ids_are_int_tuples_or_empty(raw: object, expected: tuple[int, ...]) -> None:
    assert one(FakeNode(UIA_RUNTIME_ID=raw)).runtime_id == expected


# --------------------------------------------------------------------------- #
# The constants match Windows' own type library
# --------------------------------------------------------------------------- #


def test_property_ids_match_the_uia_type_library() -> None:
    client = uia_tree._client_module()
    expected = {
        "UIA_RUNTIME_ID": "UIA_RuntimeIdPropertyId",
        "UIA_BOUNDING_RECTANGLE": "UIA_BoundingRectanglePropertyId",
        "UIA_CONTROL_TYPE": "UIA_ControlTypePropertyId",
        "UIA_NAME": "UIA_NamePropertyId",
        "UIA_HAS_KEYBOARD_FOCUS": "UIA_HasKeyboardFocusPropertyId",
        "UIA_IS_ENABLED": "UIA_IsEnabledPropertyId",
        "UIA_AUTOMATION_ID": "UIA_AutomationIdPropertyId",
        "UIA_CLASS_NAME": "UIA_ClassNamePropertyId",
        "UIA_IS_PASSWORD": "UIA_IsPasswordPropertyId",
        "UIA_IS_OFFSCREEN": "UIA_IsOffscreenPropertyId",
        "UIA_IS_VALUE_PATTERN_AVAILABLE": "UIA_IsValuePatternAvailablePropertyId",
        "UIA_VALUE_VALUE": "UIA_ValueValuePropertyId",
    }
    for ours, theirs in expected.items():
        assert getattr(uia_tree, ours) == getattr(client, theirs), ours
    assert set(PROPERTIES) == {getattr(uia_tree, name) for name in expected}
    assert client.TreeScope_Subtree == uia_tree.TREE_SCOPE_SUBTREE
    assert client.AutomationElementMode_None == uia_tree.AUTOMATION_ELEMENT_MODE_NONE


def test_roles_match_the_uia_control_type_ids() -> None:
    client = uia_tree._client_module()
    for offset, role in enumerate(ROLES):
        assert getattr(client, f"UIA_{role}ControlTypeId") == uia_tree.FIRST_CONTROL_TYPE + offset


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def _monitor(left: int, top: int, width: int, height: int, *, primary: bool) -> Monitor:
    bounds = Rect(left, top, left + width, top + height)
    return Monitor(device=f"D{left}", bounds=bounds, work_area=bounds, dpi=96, primary=primary)


DESK = DisplayLayout(
    monitors=(
        _monitor(0, 0, 1920, 1080, primary=True),
        _monitor(1920, 0, 1920, 1080, primary=False),
    ),
    virtual=Rect(0, 0, 3840, 1080),
)
UNPLUGGED = DisplayLayout(monitors=(DESK.monitors[0],), virtual=Rect(0, 0, 1920, 1080))


class _Reads:
    """Stands in for the COM half of `walk()`, recording what it was asked for."""

    def __init__(self) -> None:
        self.hwnds: list[int] = []

    def __call__(self, hwnd: int) -> tuple[tuple[UiaElement, ...], bool]:
        self.hwnds.append(hwnd)
        return flatten(FakeNode("window"))


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> _Reads:
    fake = _Reads()
    monkeypatch.setattr(uia_tree, "_read", fake)
    monkeypatch.setattr(uia_tree, "query_layout", lambda: DESK)
    monkeypatch.setattr(display, "query_layout", lambda: DESK)
    monkeypatch.setattr(display_win32, "is_window", lambda _h: True)
    return fake


def test_the_foreground_window_is_the_default(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(display_win32, "foreground_window", lambda: 4242)
    tree = walk()
    assert reads.hwnds == [4242]
    assert tree.hwnd == 4242
    assert tree.layout is DESK
    assert [e.name for e in tree.elements] == ["window"]


def test_no_foreground_window_is_refused_with_the_likely_reason(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(display_win32, "foreground_window", lambda: 0)
    with pytest.raises(UiaError, match="locked"):
        walk()
    assert reads.hwnds == []


def test_a_window_that_no_longer_exists_is_refused(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(display_win32, "is_window", lambda _h: False)
    with pytest.raises(UiaError, match="no longer exists"):
        walk(1234)
    assert reads.hwnds == []


def test_a_monitor_change_during_the_walk_voids_the_tree(
    reads: _Reads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RECOVERY.md § 3.3`: every bbox in the tree was measured on a screen that is gone."""
    monkeypatch.setattr(display, "query_layout", lambda: UNPLUGGED)
    with pytest.raises(LayoutChangedError):
        walk(1234)
    assert reads.hwnds == [1234]


def test_an_unaware_thread_is_refused_before_com_is_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UIA would hand an unaware thread scaled coordinates, with no error."""
    touched: list[int] = []
    monkeypatch.setattr(uia_tree, "_read", lambda hwnd: touched.append(hwnd))
    outcome: list[BaseException | UiaTree] = []

    def run() -> None:
        assert (
            display_win32.set_thread_dpi_awareness_context(
                display_win32.DPI_AWARENESS_CONTEXT_UNAWARE
            )
            is not None
        )
        try:
            outcome.append(walk(display_win32.foreground_window() or 1))
        except BaseException as error:  # handed back to the test thread
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(outcome[0], DpiAwarenessError)
    assert touched == []


@pytest.mark.parametrize(
    ("hresult", "message"),
    [
        (uia_tree.UIA_E_TIMEOUT, "did not answer within 2 s"),
        (uia_tree.UIA_E_ELEMENTNOTAVAILABLE, "closed while it was being read"),
        (0x80070005, r"could not read that window \(0x80070005\)"),
    ],
)
def test_a_com_failure_becomes_a_message_a_person_can_act_on(
    hresult: int, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = hresult - (1 << 32)  # `COMError.hresult` is a signed 32-bit int

    def fail(*_args: object, **_kwargs: object) -> None:
        raise comtypes.COMError(signed, "provider text that must not leak", ())

    monkeypatch.setattr(comtypes.client, "CreateObject", fail)
    with pytest.raises(UiaError, match=message) as caught:
        uia_tree._read(1234)
    assert caught.value.__cause__ is None
    assert "provider text" not in str(caught.value)


def test_the_timeout_is_short_enough_to_feel_like_a_failure_not_a_hang() -> None:
    assert TIMEOUT_MS <= 5_000


class _ComElement:
    """A cached `IUIAutomationElement`: answers from a node, never goes cross-process."""

    def __init__(self, node: UiaNode, asked: list[int]) -> None:
        self._node = node
        self._asked = asked

    def GetCachedPropertyValue(self, property_id: int) -> object:  # noqa: N802 - COM's name
        self._asked.append(property_id)
        return self._node.prop(property_id)

    def GetCachedChildren(self) -> _ComArray | None:  # noqa: N802
        children = self._node.children()
        # UIA answers "no children" with a null array, not an empty one.
        return _ComArray([_ComElement(c, self._asked) for c in children]) if children else None


class _ComArray:
    def __init__(self, items: list[_ComElement]) -> None:
        self._items = items
        self.Length = len(items)

    def GetElement(self, index: int) -> _ComElement:  # noqa: N802
        return self._items[index]


class _CacheRequest:
    def __init__(self) -> None:
        self.properties: list[int] = []
        self.TreeScope: int | None = None
        self.TreeFilter: object = None
        self.AutomationElementMode: int | None = None

    def AddProperty(self, property_id: int) -> None:  # noqa: N802
        self.properties.append(property_id)


class _Automation:
    """Records what `_read` configures before it asks for the tree."""

    ControlViewCondition = object()

    def __init__(self, root: FakeNode) -> None:
        self.root = root
        self.ConnectionTimeout: int | None = None
        self.TransactionTimeout: int | None = None
        self.request = _CacheRequest()
        self.timeouts_when_asked: tuple[int | None, int | None] | None = None
        self.hwnd: int | None = None
        self.asked: list[int] = []

    def CreateCacheRequest(self) -> _CacheRequest:  # noqa: N802
        return self.request

    def ElementFromHandleBuildCache(self, hwnd: int, request: _CacheRequest) -> _ComElement:  # noqa: N802
        assert request is self.request
        self.hwnd = hwnd
        self.timeouts_when_asked = (self.ConnectionTimeout, self.TransactionTimeout)
        return _ComElement(self.root, self.asked)


def test_read_bounds_the_call_and_fetches_the_whole_subtree_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows' default transaction timeout is 20 s: a hang, from where the user sits.

    The live hung-window test is caught at *connection*, where Windows' own default
    happens to be 2 s too, so it cannot tell whether these are set. This can.
    """
    automation = _Automation(FakeNode("window", FakeNode("ok"), FakeNode("cancel")))
    monkeypatch.setattr(comtypes.client, "CreateObject", lambda *_a, **_k: automation)
    elements, truncated = uia_tree._read(1234)
    assert automation.hwnd == 1234
    assert automation.timeouts_when_asked == (TIMEOUT_MS, TIMEOUT_MS)
    request = automation.request
    assert sorted(request.properties) == sorted(PROPERTIES)
    assert request.TreeScope == uia_tree.TREE_SCOPE_SUBTREE
    assert request.TreeFilter is _Automation.ControlViewCondition
    assert request.AutomationElementMode == uia_tree.AUTOMATION_ELEMENT_MODE_NONE
    assert not truncated
    assert [e.name for e in elements] == ["window", "ok", "cancel"]
    # Every property read came out of the cache built in that one request.
    assert set(automation.asked) <= set(PROPERTIES)


# --------------------------------------------------------------------------- #
# Live: a real window through the real UI Automation stack
# --------------------------------------------------------------------------- #


def _form_rect() -> Rect:
    work = query_layout().primary.work_area
    return Rect(work.left + 160, work.top + 160, work.left + 660, work.top + 460)


@pytest.fixture
def form() -> Iterator[FormWindow]:
    with FormWindow(_form_rect()) as window:
        yield window


def _by_name(tree: UiaTree, name: str) -> UiaElement:
    matches = [e for e in tree.elements if e.name == name]
    assert len(matches) == 1, [e.role for e in tree.elements]
    return matches[0]


def _edits(tree: UiaTree) -> tuple[UiaElement, UiaElement]:
    edits = [e for e in tree.elements if e.role == "Edit"]
    assert len(edits) == 2
    plain, password = sorted(edits, key=lambda e: e.is_password)
    return plain, password


def test_a_live_window_is_read_whole(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    assert tree.hwnd == form.hwnd
    assert not tree.truncated
    root = tree.elements[0]
    assert (root.parent, root.depth, root.name) == (None, 0, FormWindow.TITLE)
    assert root.bbox == form.rect
    assert len(tree.elements) == 5
    assert all(e.parent == 0 and e.depth == 1 for e in tree.elements[1:])
    assert all(e.runtime_id for e in tree.elements)


def test_live_controls_have_their_roles_names_values_and_state(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    button = _by_name(tree, FormWindow.BUTTON)
    assert (button.role, button.value, button.enabled) == ("Button", None, True)
    disabled = _by_name(tree, FormWindow.DISABLED_BUTTON)
    assert (disabled.role, disabled.enabled) == ("Button", False)
    plain, _ = _edits(tree)
    assert plain.value == FormWindow.TEXT


def test_live_bounding_boxes_are_physical_pixels_where_the_controls_are(
    form: FormWindow,
) -> None:
    tree = walk(form.hwnd)
    plain, password = _edits(tree)
    assert _by_name(tree, FormWindow.BUTTON).bbox == form.child_rect("button")
    assert _by_name(tree, FormWindow.DISABLED_BUTTON).bbox == form.child_rect("disabled")
    assert plain.bbox == form.child_rect("text")
    assert password.bbox == form.child_rect("password")


def test_a_live_password_box_is_flagged_and_its_contents_are_nowhere(form: FormWindow) -> None:
    tree = walk(form.hwnd)
    _, password = _edits(tree)
    assert password.is_password
    assert password.value is None
    for element in tree.elements:
        assert FormWindow.SECRET not in element.name
        assert FormWindow.SECRET not in (element.value or "")
        assert FormWindow.SECRET not in repr(element)
    assert FormWindow.SECRET not in repr(tree)


def test_a_live_walk_works_from_a_fresh_thread_and_again_on_it(form: FormWindow) -> None:
    """Each walking thread joins the COM apartment once, then reuses it."""
    outcome: list[BaseException | int] = []

    def run() -> None:
        try:
            outcome.append(len(walk(form.hwnd).elements))
            outcome.append(len(walk(form.hwnd).elements))
        except BaseException as error:  # handed back to the test thread
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert outcome == [5, 5]


def test_a_hung_live_window_fails_within_the_timeout(form: FormWindow) -> None:
    form.hang(TIMEOUT_MS / 1000 + 3)
    time.sleep(0.1)  # let the window's thread stop pumping
    started = time.monotonic()
    with pytest.raises(UiaError, match="did not answer"):
        walk(form.hwnd)
    assert time.monotonic() - started < TIMEOUT_MS / 1000 + 1.5


def test_a_closed_live_window_is_refused() -> None:
    with FormWindow(_form_rect()) as window:
        hwnd = window.hwnd
    with pytest.raises(UiaError, match="no longer exists"):
        walk(hwnd)


def test_the_live_foreground_window_can_be_read() -> None:
    hwnd = display_win32.foreground_window()
    if not hwnd:
        pytest.skip("nothing is in the foreground (locked, or a secure desktop)")
    tree = walk()
    assert tree.hwnd == hwnd
    assert tree.elements
    assert tree.elements[0].parent is None
