"""The core's half of "the core never outlives the UI" (`ARCHITECTURE.md § 3.1` step 6).

MAIN kills the core on every exit path it controls. It does not control all of
them: a MAIN killed from Task Manager, or one that dies to an access violation,
runs no cleanup at all — and what is left behind is a process with mouse control
and no window, which is exactly the failure mode `REMEMBER.md` invariant 14
refuses to ship.

So the core watches back. A daemon thread polls the supervising PID and, the
moment it is gone, stops the process. Two details are load-bearing:

* **The PID is checked together with its creation time.** Windows reuses PIDs,
  and a watch that only asks "does PID 1234 exist?" is a watch that answers yes
  about a completely different program. The identity is the pair.
* **A refused lookup is not a death.** `AccessDenied` means the process is there
  and the OS will not describe it; exiting on that would kill the core whenever
  permissions tighten. Only "no such process" counts.

The exit itself is the caller's to define — `__main__` asks uvicorn to shut down
and hard-exits if it will not — because this module knows nothing about what the
core is in the middle of.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Final

import psutil

log = logging.getLogger(__name__)

#: Step 6 gives the core 2 s to be gone. Polling at 0.5 s leaves room for the
#: shutdown itself and costs nothing measurable between ticks.
DEFAULT_POLL_INTERVAL_S: Final = 0.5


class _Unidentified:
    """The process exists but the OS will not describe it (`AccessDenied`)."""


#: A process that is gone, and one that is there but anonymous. Neither is a
#: creation time, and they mean opposite things, so they are separate values.
GONE: Final = None
UNIDENTIFIED: Final = _Unidentified()

Identity = float | _Unidentified | None


def process_identity(pid: int) -> Identity:
    """The process' creation time, `UNIDENTIFIED`, or `GONE`."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.ZombieProcess:
        # A zombie has already exited; only its table entry is left behind. It
        # is not a supervisor any more.
        return GONE
    except psutil.NoSuchProcess:
        return GONE
    except psutil.AccessDenied:
        return UNIDENTIFIED


class ParentWatch:
    """Watches the supervising process and calls `on_lost` once, when it dies."""

    def __init__(
        self,
        supervisor_pid: int,
        on_lost: Callable[[], None],
        *,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        identify: Callable[[int], Identity] = process_identity,
    ) -> None:
        self._pid = supervisor_pid
        self._on_lost = on_lost
        self._poll_interval_s = poll_interval_s
        self._identify = identify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Snapshotted now, while the supervisor is known to be the process that
        # spawned us, so a later PID reuse cannot pass for it.
        self._baseline = identify(supervisor_pid)

    def is_alive(self) -> bool:
        """Whether the supervisor this core was started by is still running."""
        current = self._identify(self._pid)
        if current is GONE:
            return False
        if isinstance(current, _Unidentified) or isinstance(self._baseline, _Unidentified):
            # Identity cannot be proven either way, but something answers to the
            # PID. Only a death gets to stop the core.
            return True
        return current == self._baseline

    def check_once(self) -> bool:
        """One poll. Fires `on_lost` and returns `False` if the supervisor is gone."""
        if self.is_alive():
            return True
        log.warning("core.supervisor_lost", extra={"supervisor_pid": self._pid})
        self._on_lost()
        return False

    def start(self) -> None:
        """Begin watching on a daemon thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="aegis-parent-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and join the thread."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=self._poll_interval_s * 4)

    def _run(self) -> None:
        # `wait` rather than `sleep`: a stopped watch must not hold shutdown up
        # for a whole poll interval.
        while not self._stop.wait(self._poll_interval_s):
            if not self.check_once():
                return
