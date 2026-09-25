"""Virtual keys by name, and key combinations from text (`P3-01`).

The agent asks for keys the way a person writes them — `ctrl+shift+n`, `alt+f4`,
`enter` — and this module is the one place that turns that into virtual-key codes.
Three rules:

- **Named keys are layout-independent.** `ctrl`, `f5`, `left`, `pagedown` mean the same
  key on every keyboard, so they are a fixed table here.
- **A single character is looked up on the user's own layout.** `ctrl+/` or `ctrl+?`
  asks Windows (`VkKeyScanW`) which key makes that character and which Shift/Ctrl/Alt
  state it needs, so `?` becomes Shift + the key that types `/` on *this* keyboard,
  and a character the layout cannot type is refused rather than guessed.
- **Nothing ambiguous is accepted.** An unknown name, an empty part, a key named
  twice, or a combination with no non-modifier key in it raises `KeyNameError`.

`is_extended()` is the other half of sending a key correctly. Windows' own
`MapVirtualKeyW` reports the arrows, Insert/Delete, Home/End and Page Up/Down with the
*numeric-keypad* scan codes they share (measured on this machine: Left is `0x4B`, not
`0xE04B`), so without Microsoft's documented extended-key list an app reading scan
codes would see numpad 4 where the agent pressed Left.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Final

# --- The keys other modules name directly -----------------------------------------

VK_BACK: Final = 0x08
VK_TAB: Final = 0x09
VK_RETURN: Final = 0x0D
VK_SHIFT: Final = 0x10
VK_CONTROL: Final = 0x11
VK_MENU: Final = 0x12  # Alt
VK_PAUSE: Final = 0x13
VK_CAPITAL: Final = 0x14
VK_ESCAPE: Final = 0x1B
VK_SPACE: Final = 0x20
VK_PRIOR: Final = 0x21  # Page Up
VK_NEXT: Final = 0x22  # Page Down
VK_END: Final = 0x23
VK_HOME: Final = 0x24
VK_LEFT: Final = 0x25
VK_UP: Final = 0x26
VK_RIGHT: Final = 0x27
VK_DOWN: Final = 0x28
VK_SNAPSHOT: Final = 0x2C  # Print Screen
VK_INSERT: Final = 0x2D
VK_DELETE: Final = 0x2E
VK_LWIN: Final = 0x5B
VK_RWIN: Final = 0x5C
VK_APPS: Final = 0x5D  # the context-menu key
VK_DIVIDE: Final = 0x6F
VK_F1: Final = 0x70
VK_NUMLOCK: Final = 0x90
VK_SCROLL: Final = 0x91
VK_LSHIFT: Final = 0xA0
VK_RSHIFT: Final = 0xA1
VK_LCONTROL: Final = 0xA2
VK_RCONTROL: Final = 0xA3
VK_LMENU: Final = 0xA4
VK_RMENU: Final = 0xA5

#: Every VK that is a modifier, including both-sided ones. `release_all()` releases
#: these last, and the preemption tests check none survives an abort.
MODIFIER_VKS: Final[frozenset[int]] = frozenset(
    {
        VK_SHIFT,
        VK_CONTROL,
        VK_MENU,
        VK_LWIN,
        VK_RWIN,
        VK_LSHIFT,
        VK_RSHIFT,
        VK_LCONTROL,
        VK_RCONTROL,
        VK_LMENU,
        VK_RMENU,
    }
)

#: Microsoft's documented extended keys ("Keystroke Message Flags"), which
#: `MapVirtualKeyW` does not all mark: right Ctrl/Alt, the navigation cluster, Num
#: Lock, Print Screen, keypad divide, and the Windows and menu keys.
EXTENDED_VKS: Final[frozenset[int]] = frozenset(
    {
        VK_RCONTROL,
        VK_RMENU,
        VK_INSERT,
        VK_DELETE,
        VK_HOME,
        VK_END,
        VK_PRIOR,
        VK_NEXT,
        VK_LEFT,
        VK_UP,
        VK_RIGHT,
        VK_DOWN,
        VK_NUMLOCK,
        VK_SNAPSHOT,
        VK_DIVIDE,
        VK_LWIN,
        VK_RWIN,
        VK_APPS,
    }
)


def is_extended(vk: int, scan: int) -> bool:
    """Whether a key needs `KEYEVENTF_EXTENDEDKEY`, given `MapVirtualKeyW`'s scan code."""
    return vk in EXTENDED_VKS or (scan >> 8) == 0xE0


# --- Names ---------------------------------------------------------------------------

_MODIFIERS: Final[Mapping[str, int]] = {
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
    "win": VK_LWIN,
    "windows": VK_LWIN,
    "lctrl": VK_LCONTROL,
    "rctrl": VK_RCONTROL,
    "lshift": VK_LSHIFT,
    "rshift": VK_RSHIFT,
    "lalt": VK_LMENU,
    "ralt": VK_RMENU,
    "altgr": VK_RMENU,
    "lwin": VK_LWIN,
    "rwin": VK_RWIN,
}

_KEYS: Final[dict[str, int]] = {
    "backspace": VK_BACK,
    "tab": VK_TAB,
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "pause": VK_PAUSE,
    "capslock": VK_CAPITAL,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
    "space": VK_SPACE,
    "pageup": VK_PRIOR,
    "pgup": VK_PRIOR,
    "pagedown": VK_NEXT,
    "pgdn": VK_NEXT,
    "end": VK_END,
    "home": VK_HOME,
    "left": VK_LEFT,
    "up": VK_UP,
    "right": VK_RIGHT,
    "down": VK_DOWN,
    "printscreen": VK_SNAPSHOT,
    "prtsc": VK_SNAPSHOT,
    "insert": VK_INSERT,
    "ins": VK_INSERT,
    "delete": VK_DELETE,
    "del": VK_DELETE,
    "apps": VK_APPS,
    "menu": VK_APPS,
    "numlock": VK_NUMLOCK,
    "scrolllock": VK_SCROLL,
    "multiply": 0x6A,
    "add": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": VK_DIVIDE,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "medianext": 0xB0,
    "mediaprevious": 0xB1,
    "mediastop": 0xB2,
    "mediaplaypause": 0xB3,
    "browserback": 0xA6,
    "browserforward": 0xA7,
    "browserrefresh": 0xA8,
}
_KEYS.update({f"f{n}": VK_F1 + n - 1 for n in range(1, 25)})
_KEYS.update({f"numpad{n}": 0x60 + n for n in range(10)})
# Letters and digits are the same VK on every layout (their ASCII codes).
_KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
_KEYS.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})

#: Every name `parse_combo()` accepts for a key that is not a modifier.
KEY_NAMES: Final[frozenset[str]] = frozenset(_KEYS)
MODIFIER_NAMES: Final[frozenset[str]] = frozenset(_MODIFIERS)

_SHIFT_STATE: Final = ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU))
_SPLIT: Final = re.compile(r"\s*\+\s*")

#: `win32.vk_for_char`, injectable so the parser is testable against any layout.
CharLookup = Callable[[str], "tuple[int, int] | None"]


class KeyNameError(ValueError):
    """A key combination that cannot be read. The message quotes only the key name."""


def _default_lookup(char: str) -> tuple[int, int] | None:
    from aegis_core.actuation import win32

    return win32.vk_for_char(char)


def parse_combo(spec: str, *, lookup: CharLookup | None = None) -> tuple[int, ...]:
    """`"ctrl+shift+n"` → the VKs to hold, in order: modifiers first, then the key.

    Case-insensitive; `+` separates parts, so the plus key itself is `add` (keypad)
    or the character after a separator (`ctrl++`). A single printable character that
    is not a named key is looked up on the current layout, and any Shift/Ctrl/Alt it
    needs is added to the modifiers.
    """
    if not spec or len(spec) > 64:
        raise KeyNameError("A key combination is 1 to 64 characters, such as ctrl+s.")
    raw = spec.strip()
    if raw == "+":
        parts = ["+"]
    elif raw.endswith("++"):
        parts = [*_SPLIT.split(raw[:-2]), "+"]  # `ctrl++`: the last `+` is the key
    else:
        parts = _SPLIT.split(raw)
    modifiers: list[int] = []
    key: int | None = None
    for part in parts:
        name = part.lower()
        if not name:
            raise KeyNameError("A key combination has an empty part.")
        if name in _MODIFIERS:
            vk = _MODIFIERS[name]
            if vk in modifiers:
                raise KeyNameError(f"'{name}' is named twice.")
            modifiers.append(vk)
            continue
        if key is not None:
            raise KeyNameError("A key combination has one key besides its modifiers.")
        if name in _KEYS:
            key = _KEYS[name]
            continue
        if len(part) == 1 and part.isprintable():
            found = (lookup or _default_lookup)(part)
            if found is None:
                raise KeyNameError(f"This keyboard layout has no key for '{part}'.")
            key, state = found
            for bit, vk in _SHIFT_STATE:
                if state & bit and vk not in modifiers:
                    modifiers.append(vk)
            continue
        raise KeyNameError(f"'{part[:24]}' is not a key Aegis knows.")
    if key is None:
        raise KeyNameError("A key combination needs a key besides its modifiers.")
    return (*modifiers, key)
