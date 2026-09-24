"""Perceptual hash of an observation: how the stuck detector tells "nothing happened".

`phash(redacted)` reduces a redacted observation to 256 bits, and two hashes are
compared by how many bits differ. The stuck detector (`P4-06`,
`ARCHITECTURE.md § 6.1`) calls three observations in a row *near-identical* when
each is within `NEAR_IDENTICAL_BITS` of the last; the hex form is what
`observations.phash` stores.

The hash is a **difference hash** (dHash): the frame is shrunk to a 17x16 grey
grid, and each bit says whether a cell is clearly brighter — by more than
`DEAD_BAND` levels — than its right-hand neighbour. It is built for UI screens,
which are mostly flat colour:

* **A static screen hashes the same every time.** Neighbouring flat cells are
  equal or nearly so, and the dead band keeps a one-level rounding difference
  between them from deciding a bit. Measured on a real 3200x2000 desktop: six
  captures 0.6 s apart, distance 0.
* **Noise stays under the threshold.** Without the band, ±2 levels of noise on
  every pixel moved 4 bits of a UI-shaped frame, all of them ties between flat
  cells; with it, noise up to ±8 moved none.
* **What the agent causes clears it.** Painted onto a real frame: a 500x700
  dropdown moved 9 bits, a 1200x800 dialog 33. A tooltip moved 2 and a line of
  text none — both within `NEAR_IDENTICAL_BITS`, which is the point: the detector asks
  whether the screen *meaningfully* changed, and a stuck call also needs the plan
  to be unchanged. A caret, a typed character and a toggled checkbox move
  nothing at this grid; a finer one would see them, but would then need a
  threshold that noise crosses.

It hashes a **`Redacted`**, never a `Frame`: the hash is persisted, and nothing
derived from pixels redaction has not cleared is persisted. It reads the frame
through `Frame._to_image` — the same private downscale `mark.py` uses, allowed
here by `tests/perception/test_redact.py`'s source scan — and keeps nothing but
the 256 bits, from which no text can be recovered. It costs ~11 ms on a
3200x2000 frame.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Final

from PIL import Image

from aegis_core.perception.redact import Redacted

log = logging.getLogger(__name__)

#: The grey grid the frame is shrunk to. Each row gives `GRID_WIDTH - 1` bits.
GRID_WIDTH: Final = 17
GRID_HEIGHT: Final = 16

#: 16 bits a row, 16 rows.
HASH_BITS: Final = (GRID_WIDTH - 1) * GRID_HEIGHT

#: Two observations at most this many bits apart show the same screen. Noise was
#: measured at 0 bits and at most 3 without the dead band; the smallest change
#: that should count — a dropdown opening — at 9.
NEAR_IDENTICAL_BITS: Final = 5

#: How many grey levels brighter a cell must be than its neighbour — more than —
#: for its bit to be set. One level is what a flat region's cells differ by when
#: box averages round differently.
DEAD_BAND: Final = 1

#: The long edge of the first, colour downscale. Shrinking a 3200x2000 frame
#: straight to grey would convert all 6.4 M pixels; shrinking first converts a
#: few thousand. Eight cells per grid cell keeps the second box filter an average
#: of real pixels, not of one.
_FIRST_EDGE: Final = GRID_WIDTH * 8

_HEX: Final = re.compile(rf"[0-9a-f]{{{HASH_BITS // 4}}}")


@dataclass(frozen=True, slots=True)
class PerceptualHash:
    """256 bits describing how an observation looks. Compare with `distance()`."""

    bits: int

    def __post_init__(self) -> None:
        if not 0 <= self.bits < 1 << HASH_BITS:
            raise ValueError(f"A perceptual hash has {HASH_BITS} bits.")

    def distance(self, other: PerceptualHash) -> int:
        """How many of the 256 bits differ: 0 for the same screen, larger for more change."""
        return (self.bits ^ other.bits).bit_count()

    def near_identical(self, other: PerceptualHash) -> bool:
        """Whether `other` shows the same screen, as far as the stuck detector cares."""
        return self.distance(other) <= NEAR_IDENTICAL_BITS

    @property
    def hex(self) -> str:
        """Fixed-width lowercase hex: the form `observations.phash` stores."""
        return f"{self.bits:0{HASH_BITS // 4}x}"

    @classmethod
    def from_hex(cls, text: str) -> PerceptualHash:
        """Read back what `hex` wrote. Anything else — another width, case or hash — is refused."""
        if _HEX.fullmatch(text) is None:
            raise ValueError(f"A perceptual hash is {HASH_BITS // 4} lowercase hex digits.")
        return cls(int(text, 16))


def phash(redacted: Redacted) -> PerceptualHash:
    """The perceptual hash of `redacted`'s frame, as redaction left it."""
    started = time.perf_counter()
    grid = (
        redacted.frame._to_image(_FIRST_EDGE)
        .convert("L")
        .resize((GRID_WIDTH, GRID_HEIGHT), Image.Resampling.BOX)
    )
    result = PerceptualHash(difference_bits(grid))
    log.debug("screen.hashed", extra={"ms": round((time.perf_counter() - started) * 1000, 1)})
    return result


def difference_bits(grid: Image.Image) -> int:
    """One bit per horizontally adjacent pair of `grid`'s grey cells, row by row:
    1 where the left cell is brighter by more than `DEAD_BAND`."""
    if grid.mode != "L" or grid.size != (GRID_WIDTH, GRID_HEIGHT):
        raise ValueError(f"Expected a {GRID_WIDTH}x{GRID_HEIGHT} grey grid.")
    cells = grid.tobytes()
    bits = 0
    for row in range(GRID_HEIGHT):
        start = row * GRID_WIDTH
        for left, right in zip(
            cells[start : start + GRID_WIDTH - 1],
            cells[start + 1 : start + GRID_WIDTH],
            strict=True,
        ):
            bits = (bits << 1) | (left > right + DEAD_BAND)
    return bits
