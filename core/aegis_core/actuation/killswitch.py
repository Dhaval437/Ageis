"""The core's half of the kill switch (`P3-06`, `ARCHITECTURE.md § 8.3`).

The hotkey lives in Electron MAIN, so a hung core cannot disable it
(`REMEMBER.md` invariant 2). MAIN first asks the core to stop — `POST /v1/kill`,
which lands here — and if the core does not answer inside its deadline, MAIN
terminates the process instead. This module is therefore the *polite* half: it
is what runs when the core is healthy enough to answer, and it is why a healthy
core survives the kill switch with its journal and timeline intact.

`engage()` does three things, in this order:

1. **Freezes.** Every `InputController` attached to this switch checks it before
   and between every event, so no sequence can start or continue once it is set.
2. **Releases.** Every attached controller's `release_all()` runs, even if one
   of them fails — a stuck modifier is a P0 (invariant 3), and one controller's
   failing `KEYUP` must not strand another's.
3. **Stays engaged.** A kill is not a pause. Nothing in the core clears it except
   `reset()`, which only the start of a *new* task may call (`P4-02`).

Unlike preemption, there is no hook thread here and no latency budget inside the
core: the < 200 ms target is measured from MAIN, and MAIN stops waiting for this
module after `KILL_ACK_TIMEOUT_MS` whatever it is doing.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class Releasable(Protocol):
    """What the switch needs from an `InputController`. A protocol, not an import,
    because `input.py` imports this module."""

    def release_all(self) -> tuple[tuple[int, ...], tuple[object, ...]]: ...


@dataclass(frozen=True, slots=True)
class KillReport:
    """What one `engage()` did. Counts only — nothing a key name or a path could be in."""

    released_keys: int
    released_buttons: int
    #: Controllers whose `release_all()` raised. Non-zero means a key may be held,
    #: and MAIN's own release path (`P3-15`) is the remaining line of defence.
    release_failures: int


class KillSwitch:
    """A process-wide, sticky "stop everything" flag with a release sweep.

    Thread-safe: `engage()` arrives on the event loop (the route) while the
    controllers it releases are being driven from the agent's thread.
    """

    def __init__(self) -> None:
        self._engaged = threading.Event()
        self._lock = threading.Lock()
        # Weak: a controller that is gone holds no keys, and the switch must not
        # be the thing that keeps it alive.
        self._controllers: weakref.WeakSet[Releasable] = weakref.WeakSet()

    @property
    def engaged(self) -> bool:
        return self._engaged.is_set()

    def attach(self, controller: Releasable) -> None:
        """Put a controller under this switch. `InputController` does it itself."""
        with self._lock:
            self._controllers.add(controller)

    def engage(self) -> KillReport:
        """Freeze, then release every key and button any attached controller holds.

        Idempotent: engaging an engaged switch sweeps again, which is harmless and
        is exactly what a second press of the hotkey should do.
        """
        # Set first: a sequence between two events must see the flag before its
        # next event, not after the sweep has released what it was holding.
        self._engaged.set()
        with self._lock:
            controllers = list(self._controllers)

        keys = buttons = failures = 0
        for controller in controllers:
            try:
                released, released_buttons = controller.release_all()
            except Exception:  # every controller still gets its sweep
                failures += 1
                log.exception("kill_switch.release_failed")
            else:
                keys += len(released)
                buttons += len(released_buttons)

        report = KillReport(keys, buttons, failures)
        log.warning(
            "kill_switch.engaged",
            extra={
                "controllers": len(controllers),
                "released_keys": keys,
                "released_buttons": buttons,
                "release_failures": failures,
            },
        )
        return report

    def reset(self) -> None:
        """Re-arm. Only the start of a new task may call this — never a resume."""
        self._engaged.clear()
