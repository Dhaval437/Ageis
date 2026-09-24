"""Redaction: the only way a captured frame becomes something that can leave the process.

`redact(frame, trees)` takes a full-resolution `Frame` and the UI trees walked
for it, and returns a `Redacted` observation: a copy of the frame with secrets
painted black **in the pixel bytes**, the trees with secret text removed, and the
one method that encodes the frame into a `Screenshot` a model can be sent
(invariant 8, `ARCHITECTURE.md § 8.4`).

What is secret:

* **Fields.** A password field (`IsPassword`), and a field whose label reads like
  a secret — password, PIN, OTP, CVV, seed phrase, recovery key and the rest of
  `SECRET_LABEL` — whether the label is the field's own name or an unnamed
  field's preceding text. Its value, and everything inside it, is dropped from the
  tree and its rectangle is blacked out. It stays in the tree, flagged
  `redacted`, so the agent knows to stop and let the human type (invariant 7).
* **Credential-shaped text** anywhere — an API key, a private-key header, a JWT,
  a card number that passes Luhn. It is replaced in the tree and the element that
  showed it is blacked out, because the pixels cannot be edited word by word.

What is vouched for: a tree describes one window, but a frame shows whatever is
on screen. So **every pixel no walked window vouches for is black**: anything
outside the walked windows, and any part of one covered by a window stacked
above it that was not walked — a password manager's popup, a toast, another
app's dialog. A tree the walk had to truncate vouches for nothing, since the
field it missed may be the password. The model can only act on what is in a
tree anyway; showing it pixels it cannot address buys nothing and risks the one
thing this module exists to stop.

What no tree describes: a window that draws its own content — a canvas, a
terminal buffer, an Electron page with accessibility off — shows text its tree
does not have, so none of the rules above can see it. `ocr.blind_regions()`
finds those parts of each walked window, and they are **read or blacked out,
never sent unread**: after the tree-based painting, OCR reads each one off the
already-painted frame (so it cannot read a secret field or another window), and
every line goes through the same rules — credential-shaped text, and a line
carrying a secret label, is replaced in the text and blacked out in the pixels.
A region OCR cannot read — no engine, no OCR language installed, a failure or a
timeout, or a caller passing `ocr=None` — is painted black and listed in
`Redacted.unread`. OCR's own misreads are the residual risk there: a key it
splits with a space no longer matches its pattern.

When redaction refuses: a tree measured under a different display layout than
the frame, or a window that has moved since it was walked, would put every
black box in the wrong place. Both raise `RedactionError`; the caller observes
again. What this cannot see is a field that scrolled *inside* a window that did
not move, in the ~100 ms between walk and capture; boxes are padded, and that is
the residual risk.

Nothing here logs screen text: only counts.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from aegis_core.perception import win32
from aegis_core.perception.display import Rect
from aegis_core.perception.ocr import (
    MAX_LINES,
    SYSTEM_OCR,
    OcrEngine,
    OcrError,
    OcrLine,
    blind_regions,
    read_region,
    shorten,
)
from aegis_core.perception.screen import (
    BYTES_PER_PIXEL,
    MAX_EDGE,
    WEBP_QUALITY,
    Frame,
    Screenshot,
)
from aegis_core.perception.uia_tree import UiaElement, UiaTree

log = logging.getLogger(__name__)

#: What replaces credential-shaped text in the tree.
REDACTED: Final = "[redacted]"

#: Physical pixels added round every blacked-out element: its focus ring, the
#: caret, a glyph overhanging its box, and a little of the scroll race.
PAD_PX: Final = 4

#: `REVIEW.md § 2`: bounded. A busy desktop has a few hundred top-level windows,
#: most of them invisible.
MAX_WINDOWS: Final = 4_096

#: A label that marks the field it names as secret. Matched against field names
#: only, so a *Pin to taskbar* button is not a field and is left alone.
SECRET_LABEL: Final = re.compile(
    r"""\b(
        pass(?:word|wd|code|phrase)s? | pwd
      | pin(?:\s*(?:code|number))?
      | otp | totp | one[\s-]?time\s+(?:code|password|passcode|pin)
      | (?:verification|security|auth(?:entication)?|2fa|mfa|sms|access)\s+code
      | cvv2? | cvc2? | csc | card\s+security
      | seed(?:\s+phrase)? | mnemonic | recovery\s+(?:key|phrase|codes?) | backup\s+codes?
      | secret(?:\s+key)? | private\s+key | api[\s_-]?key | access\s+(?:key|token)
      | auth(?:entication)?\s+token | client\s+secret | bearer
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

#: Text shaped like a credential, wherever it appears.
CREDENTIAL: Final = re.compile(
    r"""
        \bsk-(?:ant-|or-|proj-)?[A-Za-z0-9_-]{20,}       # OpenAI, Anthropic, OpenRouter
      | \bnvapi-[A-Za-z0-9_-]{20,}                        # NVIDIA
      | \bAIza[0-9A-Za-z_-]{35}                           # Google
      | \b(?:AKIA|ASIA)[0-9A-Z]{16}\b                     # AWS access key id
      | \bgh[pousr]_[A-Za-z0-9]{30,}                      # GitHub
      | \bgithub_pat_[A-Za-z0-9_]{30,}
      | \bglpat-[A-Za-z0-9_-]{20,}                        # GitLab
      | \bxox[abposr]-[A-Za-z0-9-]{10,}                   # Slack
      | \b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}            # Stripe
      | \beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}   # JWT
      | -----BEGIN[A-Z ]*PRIVATE\ KEY-----
    """,
    re.VERBOSE,
)

#: Thirteen to nineteen digits, optionally grouped by single spaces or hyphens.
_CARD_CANDIDATE: Final = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


class RedactionError(RuntimeError):
    """The frame and its trees do not describe the same screen; observe again."""


@dataclass(frozen=True, slots=True)
class OnScreenWindow:
    """A visible top-level window as the redactor needs it.

    `rect` is `GetWindowRect`, which is what a walked tree's root reports, so a
    difference means the window moved since the walk. `bounds` is the window as
    drawn, which is what covers the pixels beneath it.
    """

    hwnd: int
    rect: Rect
    bounds: Rect


@dataclass(frozen=True, slots=True, eq=False)
class Redacted:
    """A frame and its trees after redaction. `encode()` is the way out of the process."""

    frame: Frame
    trees: tuple[UiaTree, ...]
    #: Physical rectangles painted black, clipped to the frame.
    boxes: tuple[Rect, ...] = field(repr=False)
    #: Text OCR read in the parts of the windows their trees do not describe,
    #: after redaction, top to bottom within each region.
    text: tuple[OcrLine, ...] = field(default=(), repr=False)
    #: Parts of the windows no tree describes that could not be read, and so were
    #: painted black. The model is shown nothing there.
    unread: tuple[Rect, ...] = ()

    def encode(self, *, max_edge: int = MAX_EDGE, quality: int = WEBP_QUALITY) -> Screenshot:
        """The redacted frame as the WebP a model is shown."""
        return self.frame._encode(max_edge=max_edge, quality=quality)


def redact(
    frame: Frame,
    trees: Sequence[UiaTree],
    *,
    stacking: Sequence[OnScreenWindow] | None = None,
    ocr: OcrEngine | None = SYSTEM_OCR,
) -> Redacted:
    """`frame` and `trees` with every secret removed, and every unvouched pixel black.

    `trees` are the windows walked for this observation, usually just the
    foreground one. `stacking` is the visible top-level windows, topmost first;
    by default it is read from Windows now, which is what makes a window that
    has moved since its walk detectable. `ocr` reads the parts of those windows
    their trees do not describe; `None` paints them black instead. Raises
    `RedactionError` if a tree does not match the frame's layout or its window's
    current position.
    """
    started = time.perf_counter()
    for tree in trees:
        if tree.layout != frame.layout:
            raise RedactionError(
                "The screen layout changed between reading a window and capturing it."
            )
    windows = list(stacking) if stacking is not None else _stacking()
    cleaned: list[UiaTree] = []
    boxes: list[Rect] = []
    for tree in trees:
        clean, secret = redact_tree(tree)
        cleaned.append(clean)
        boxes.extend(_pad(rect) for rect in secret)
    boxes.extend(_unvouched(frame.region, trees, windows))
    painted = [clip for rect in boxes if (clip := _intersect(rect, frame.region)) is not None]
    first = _painted(frame, painted)
    text, secret_lines, unread = _read_blind(first, cleaned, ocr)
    more = [clip for rect in secret_lines if (clip := _intersect(rect, frame.region)) is not None]
    more.extend(unread)
    result = Redacted(
        frame=_painted(first, more),
        trees=tuple(cleaned),
        boxes=tuple(painted + more),
        text=text,
        unread=tuple(unread),
    )
    log.debug(
        "screen.redacted",
        extra={
            "trees": len(trees),
            "boxes": len(painted) + len(more),
            "ocr_lines": len(text),
            "unread": len(unread),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return result


def redact_text(text: str) -> str:
    """`text` with every credential-shaped run replaced by `REDACTED`.

    The same scanner the tree goes through; `P5-12`'s clipboard reads use it.
    """
    text = CREDENTIAL.sub(REDACTED, text)
    return _CARD_CANDIDATE.sub(_card_or_not, text)


def is_secret_label(label: str) -> bool:
    return SECRET_LABEL.search(label) is not None


def redact_tree(tree: UiaTree) -> tuple[UiaTree, list[Rect]]:
    """`tree` with secret content removed, and the rectangles whose pixels must go.

    Document order means a parent is always decided before its children, so
    "inside a secret field" is one lookup.
    """
    elements = tree.elements
    inside_secret: dict[int, bool] = {}
    previous_sibling: dict[int | None, UiaElement] = {}
    out: list[UiaElement] = []
    boxes: list[Rect] = []
    for e in elements:
        parent = e.parent
        inherited = parent is not None and inside_secret[parent]
        label = previous_sibling.get(parent)
        previous_sibling[parent] = e
        secret_field = _is_field(e) and (
            e.is_password
            or is_secret_label(e.name)
            or (
                not e.name
                and label is not None
                and label.role == "Text"
                and is_secret_label(label.name)
            )
        )
        inside_secret[e.id] = inherited or secret_field
        if inherited:
            cleaned = replace(e, name="", value=None, redacted=True)
        elif secret_field:
            cleaned = replace(e, name=redact_text(e.name), value=None, redacted=True)
        else:
            name = redact_text(e.name)
            value = None if e.value is None else redact_text(e.value)
            changed = name != e.name or value != e.value
            cleaned = replace(e, name=name, value=value, redacted=True) if changed else e
        if cleaned.redacted and e.bbox is not None:
            boxes.append(e.bbox)
        out.append(cleaned)
    return replace(tree, elements=tuple(out), redacted=True), boxes


# --------------------------------------------------------------------------- #
# What no tree describes
# --------------------------------------------------------------------------- #


def _read_blind(
    frame: Frame, trees: Sequence[UiaTree], engine: OcrEngine | None
) -> tuple[tuple[OcrLine, ...], list[Rect], list[Rect]]:
    """The redacted OCR text of every blind region of `trees`, the padded boxes of
    the lines that carried a secret, and the regions that could not be read.

    `frame` is already painted, so OCR never sees a secret field or a pixel no
    tree vouches for. Every line is scanned before the cap drops any.
    """
    lines: list[OcrLine] = []
    secret: list[Rect] = []
    unread: list[Rect] = []
    for tree in trees:
        for region in blind_regions(tree):
            clip = _intersect(region, frame.region)
            if clip is None:
                continue
            if engine is None:
                unread.append(clip)
                continue
            try:
                found = read_region(engine, _crop(frame, clip), clip)
            except OcrError as error:
                log.warning("screen.ocr_unread", extra={"reason": type(error).__name__})
                unread.append(clip)
                continue
            for line in found:
                cleaned = _redact_line(line)
                if cleaned.redacted:
                    secret.append(_pad(line.bbox))
                lines.append(cleaned)
    return tuple(lines[:MAX_LINES]), secret, unread


def _redact_line(line: OcrLine) -> OcrLine:
    """`line` with any secret removed and flagged, its text cut to length.

    A line with a secret label loses all of its text: the value may be anywhere
    in it. A line with only a credential in it keeps the words round it. The
    scan runs on the whole line, before it is cut.
    """
    if is_secret_label(line.text):
        return replace(line, text=REDACTED, redacted=True)
    text = redact_text(line.text)
    if text != line.text:
        return replace(line, text=shorten(text), redacted=True)
    return replace(line, text=shorten(text))


# --------------------------------------------------------------------------- #
# What the trees vouch for
# --------------------------------------------------------------------------- #


def _unvouched(
    region: Rect, trees: Sequence[UiaTree], windows: Sequence[OnScreenWindow]
) -> list[Rect]:
    """Rectangles of `region` no tree vouches for: outside every walked window, or under
    an unwalked window stacked above one."""
    walked = {tree.hwnd for tree in trees}
    position = {w.hwnd: i for i, w in enumerate(windows)}
    vouched: list[Rect] = []
    covered: list[Rect] = []
    for tree in trees:
        index = position.get(tree.hwnd)
        if index is None:
            raise RedactionError("A walked window is not a visible top-level window any more.")
        window = windows[index]
        root = tree.elements[0].bbox if tree.elements else None
        if root != window.rect:
            raise RedactionError("A window moved after it was read.")
        if tree.truncated:
            continue
        vouched.append(window.bounds)
        for above in windows[:index]:
            if above.hwnd not in walked and (hidden := _intersect(above.bounds, window.bounds)):
                covered.append(hidden)
    return _subtract([region], vouched) + covered


def _stacking() -> list[OnScreenWindow]:
    """Every visible top-level window, topmost first, as Windows reports it now."""
    windows: list[OnScreenWindow] = []
    for hwnd in win32.enum_windows(MAX_WINDOWS):
        if not win32.is_window_visible(hwnd) or win32.is_minimised(hwnd) or win32.is_cloaked(hwnd):
            continue
        rect = win32.window_rect(hwnd)
        bounds = win32.window_bounds(hwnd)
        if rect is None or bounds is None:
            continue
        windows.append(
            OnScreenWindow(
                hwnd=hwnd,
                rect=Rect(rect.left, rect.top, rect.right, rect.bottom),
                bounds=Rect(
                    int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom)
                ),
            )
        )
    return windows


# --------------------------------------------------------------------------- #
# Painting
# --------------------------------------------------------------------------- #


def _painted(frame: Frame, rects: Sequence[Rect]) -> Frame:
    """A copy of `frame` with `rects` black. The original frame is never touched."""
    if not rects:
        return frame
    pixels = bytearray(frame.pixels)
    stride = frame.width * BYTES_PER_PIXEL
    origin = frame.region
    for rect in rects:
        start = (rect.left - origin.left) * BYTES_PER_PIXEL
        span = rect.width * BYTES_PER_PIXEL
        black = bytes(span)
        for y in range(rect.top - origin.top, rect.bottom - origin.top):
            offset = y * stride + start
            pixels[offset : offset + span] = black
    return Frame(
        region=frame.region, layout=frame.layout, captured_at=frame.captured_at, pixels=pixels
    )


def _crop(frame: Frame, rect: Rect) -> bytes:
    """The top-down BGRX bytes of `rect`, which must lie inside `frame`."""
    stride = frame.width * BYTES_PER_PIXEL
    start = (rect.left - frame.region.left) * BYTES_PER_PIXEL
    span = rect.width * BYTES_PER_PIXEL
    rows = range(rect.top - frame.region.top, rect.bottom - frame.region.top)
    return b"".join(frame.pixels[y * stride + start : y * stride + start + span] for y in rows)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_field(e: UiaElement) -> bool:
    """Something a person types into: an edit or combo box, or anything holding a value."""
    return e.role in ("Edit", "ComboBox") or e.value is not None or e.is_password


def _card_or_not(match: re.Match[str]) -> str:
    digits = [int(c) for c in match.group() if c.isdigit()]
    return REDACTED if _luhn(digits) else match.group()


def _luhn(digits: Sequence[int]) -> bool:
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _pad(rect: Rect) -> Rect:
    return Rect(rect.left - PAD_PX, rect.top - PAD_PX, rect.right + PAD_PX, rect.bottom + PAD_PX)


def _intersect(a: Rect, b: Rect) -> Rect | None:
    rect = Rect(
        max(a.left, b.left), max(a.top, b.top), min(a.right, b.right), min(a.bottom, b.bottom)
    )
    return None if rect.is_empty else rect


def _subtract(pieces: Iterable[Rect], cuts: Iterable[Rect]) -> list[Rect]:
    """What is left of `pieces` once every rectangle in `cuts` is taken out."""
    remaining = list(pieces)
    for cut in cuts:
        next_round: list[Rect] = []
        for piece in remaining:
            overlap = _intersect(piece, cut)
            if overlap is None:
                next_round.append(piece)
                continue
            # Up to four bands round the overlap: above, below, left, right.
            bands = (
                Rect(piece.left, piece.top, piece.right, overlap.top),
                Rect(piece.left, overlap.bottom, piece.right, piece.bottom),
                Rect(piece.left, overlap.top, overlap.left, overlap.bottom),
                Rect(overlap.right, overlap.top, piece.right, overlap.bottom),
            )
            next_round.extend(band for band in bands if not band.is_empty)
        remaining = next_round
    return remaining
