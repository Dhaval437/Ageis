"""Timing a piece of perception against the machine's own speed (`P2-14`, `P2-15`).

A wall-clock ceiling on a developer's machine is not a gate. Measured here, on 8
logical CPUs with 0 to 20 busy processes: drawing a page of marks took 38 to 383 ms,
a perceptual hash 14 to 110 ms and a prune of the largest tree 65 to 286 ms — with
the code unchanged, and each had a ceiling (150, 50, 200 ms) inside that range. On
battery with the owner working, all three missed in one run and passed in the next.

So the default suite asks a different question: how much does the work cost **in
units of a fixed calibration workload run in the same moments**? Load, turbo clocks
and battery throttling slow both alike, and `pace()` takes each alternating pair's
ratio before the median, so load that comes and goes during the test cancels out
instead of landing on one side. Across the same loads the three median ratios
stayed within 0.48 to 0.65, 0.18 to 0.31 and 0.80 to 1.02.

Each ceiling is twice the worst honest reading, so load cannot reach it and a
regression of that size cannot hide. One of about half that size can: a ratio gate
catches the mistakes that matter (a lost downscale, a quadratic walk), not a slow
drift. The absolute budgets are still asserted, as benchmarks, when `AEGIS_PERF=1`
says the machine is quiet (`perf_benchmark`); `P8-04` runs them.
"""

from __future__ import annotations

import functools
import os
import random
import statistics
import zlib
from collections.abc import Callable
from time import perf_counter

import pytest

#: Set to run the wall-clock benchmarks. A quiet machine on mains power, or they lie.
PERF_ENV = "AEGIS_PERF"

perf_benchmark = pytest.mark.skipif(
    not os.environ.get(PERF_ENV),
    reason=f"set {PERF_ENV}=1 on a quiet machine to run the wall-clock benchmarks",
)

#: How many calibration/work pairs one reading takes. Odd, so the median is a pair.
ROUNDS = 7


@functools.cache
def calibration_text() -> bytes:
    """~1 MB of seeded word-like text for zlib: the same bytes every run, and work
    the CPU does the same way the encode does (a C loop over a large buffer), so
    load, turbo clocks and battery throttling slow both alike."""
    rnd = random.Random(5)  # noqa: S311 - a fixed workload for a timing test, not secrets
    words = [
        bytes(rnd.choice(b"abcdefghij ") for _ in range(rnd.randint(3, 9))) for _ in range(500)
    ]
    return b" ".join(rnd.choice(words) for _ in range(250_000))[:1_000_000]


def calibrate() -> None:
    """The fixed workload every ratio is measured in: zlib, level 6, over `calibration_text()`."""
    zlib.compress(calibration_text(), 6)


def pace(work: Callable[[], object], *, rounds: int = ROUNDS) -> list[float]:
    """What `work` costs in units of the calibration: one ratio per alternating pair.

    Both are run once first, untimed, so neither pays for a first-call setup (a
    codec's tables, a font, zlib's own) inside a measurement.
    """
    work()
    calibrate()
    ratios: list[float] = []
    for _ in range(rounds):
        started = perf_counter()
        calibrate()
        calibration = perf_counter() - started
        started = perf_counter()
        work()
        ratios.append((perf_counter() - started) / calibration)
    return ratios


def assert_keeps_pace(work: Callable[[], object], ceiling: float) -> None:
    """Fail unless the median of `work`'s ratios is under `ceiling`; show them all if not."""
    ratios = pace(work)
    median = statistics.median(ratios)
    assert median < ceiling, f"{median:.2f} x the calibration (ceiling {ceiling}): {ratios}"


def wall_clock_ms(work: Callable[[], object], *, rounds: int = ROUNDS) -> float:
    """The median wall-clock time of `work`, in ms, after one untimed call."""
    work()
    timings: list[float] = []
    for _ in range(rounds):
        started = perf_counter()
        work()
        timings.append((perf_counter() - started) * 1000)
    return statistics.median(timings)
