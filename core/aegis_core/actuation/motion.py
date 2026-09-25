"""Human-like pointer motion (`P3-03`): a path, a pace, and a pause before clicking.

Many interfaces only work for a pointer that *travels*. A menu opens on hover, a
toolbar shows its buttons when the cursor enters, a link reveals its target: all of
that is driven by `WM_MOUSEMOVE` along the way and by the cursor resting for a moment
before the press. A cursor that teleports and clicks in the same millisecond gets
none of it, and the click lands on a control that has not finished appearing.

So a move is planned as a person makes one, within bounds chosen for an agent that
still has work to do:

- **The path is a cubic Bézier curve** from where the cursor is to the target, with
  its two control points pushed sideways by a random amount — at most `curvature`
  of the distance — so it bows the way a wrist does rather than ruling a line.
- **The pace eases in and out** (smoothstep), so steps are short at both ends, and
  the whole move takes a Fitts'-law time for its distance, clamped to
  `[min_duration, max_duration]`.
- **Jitter** of at most `jitter_px` on the points in between — never on the last one.
  **The path always ends exactly on the target**: the point was grounded to a pixel
  (`perception/grounding.py`) and the click has to land on it.
- **Dwell** before a press: a random pause in `dwell`, long enough for a hover state
  to settle.

Pure: `plan_path()` takes the random generator, so a test can fix its seed. Whether a
path is on screen is `input.py`'s question, answered against the display layout
before anything moves.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from aegis_core.perception.display import Point

#: Fitts' law, `a + b * log2(1 + distance / width)`: the constants of a quick, sure
#: hand rather than a careful one — a 1000 px move takes ~0.4 s, a 100 px move ~0.2 s.
_FITTS_A: Final = 0.05
_FITTS_B: Final = 0.06
#: The target width Fitts' law is computed against: a typical button's short side.
_TARGET_PX: Final = 20

#: Closer than this, the cursor is simply put on the target: there is nothing to travel.
MIN_TRAVEL_PX: Final = 3


@dataclass(frozen=True, slots=True)
class Motion:
    """How a pointer travels. `HUMAN` is the default the agent uses."""

    min_duration: float = 0.08
    max_duration: float = 0.45
    #: Seconds between two points of the path: ~120 moves a second, what a mouse reports.
    step_interval: float = 0.008
    #: The furthest the curve may bow from the straight line, as a share of its length.
    curvature: float = 0.15
    jitter_px: int = 1
    #: Seconds the cursor rests on the target before a press, drawn uniformly.
    dwell: tuple[float, float] = (0.04, 0.12)
    #: A hard cap on points per move, whatever the timings above say.
    max_steps: int = 80

    def __post_init__(self) -> None:
        if not 0 < self.min_duration <= self.max_duration:
            raise ValueError("durations must satisfy 0 < min_duration <= max_duration")
        if self.step_interval <= 0 or self.max_steps < 2:
            raise ValueError("step_interval must be positive and max_steps at least 2")
        if not 0 <= self.curvature <= 0.5 or self.jitter_px < 0:
            raise ValueError("curvature must be in [0, 0.5] and jitter_px not negative")
        if not 0 <= self.dwell[0] <= self.dwell[1]:
            raise ValueError("dwell must be an ordered pair of non-negative seconds")

    def straight(self) -> Motion:
        """The same pace, on a straight line with no jitter: the fallback path."""
        return Motion(
            self.min_duration,
            self.max_duration,
            self.step_interval,
            0.0,
            0,
            self.dwell,
            self.max_steps,
        )


HUMAN: Final = Motion()


def duration(distance: float, motion: Motion) -> float:
    """How long a move of `distance` pixels takes, by Fitts' law, clamped."""
    seconds = _FITTS_A + _FITTS_B * math.log2(1 + distance / _TARGET_PX)
    return min(motion.max_duration, max(motion.min_duration, seconds))


def dwell(motion: Motion, rng: random.Random) -> float:
    """The pause before a press."""
    return rng.uniform(*motion.dwell)


def _ease(t: float) -> float:
    """Smoothstep: slow away from the start, slow into the target."""
    return t * t * (3 - 2 * t)


def plan_path(start: Point, end: Point, motion: Motion, rng: random.Random) -> list[Point]:
    """The points to move through after `start`, ending exactly on `end`.

    Returns `[end]` for a move shorter than `MIN_TRAVEL_PX`. Consecutive duplicates
    are dropped, so every point is a move Windows will actually report.
    """
    dx, dy = end.x - start.x, end.y - start.y
    distance = math.hypot(dx, dy)
    if distance < MIN_TRAVEL_PX:
        return [end]
    steps = max(2, min(motion.max_steps, round(duration(distance, motion) / motion.step_interval)))
    # A unit vector across the line, and two independent bows along it.
    nx, ny = -dy / distance, dx / distance
    bow1 = rng.uniform(-motion.curvature, motion.curvature) * distance
    bow2 = rng.uniform(-motion.curvature, motion.curvature) * distance
    c1 = (start.x + dx * 0.3 + nx * bow1, start.y + dy * 0.3 + ny * bow1)
    c2 = (start.x + dx * 0.7 + nx * bow2, start.y + dy * 0.7 + ny * bow2)

    path: list[Point] = []
    for index in range(1, steps):
        t = _ease(index / steps)
        u = 1 - t
        x = u**3 * start.x + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * end.x
        y = u**3 * start.y + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * end.y
        jitter = motion.jitter_px
        point = Point(
            round(x) + rng.randint(-jitter, jitter), round(y) + rng.randint(-jitter, jitter)
        )
        if not path or point != path[-1]:
            path.append(point)
    if path and path[-1] == end:
        path.pop()
    path.append(end)
    return path
