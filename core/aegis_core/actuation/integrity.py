"""Whether the focused window is walled off from us by UIPI (`P3-20`).

Windows' User Interface Privilege Isolation stops a process from sending input to
a window of a higher integrity level: Task Manager, an elevated terminal, an
installer that asked to run as administrator. Measured on this project's machine
with Task Manager in front (`PROGRESS.md`, P3-20):

* `SendInput` reports success, and **nothing lands** — no key, no cursor move.
* Our low-level hooks are **not called** — not for our input, and not for the
  human's: forty seconds of real typing and mouse movement produced no event.
* A key AEGIS pressed *before* the window came forward stays down: its `KEYUP`
  is dropped like everything else, and `GetAsyncKeyState` reads every key as up
  until the window loses focus, so the stuck key cannot even be seen.

So behind such a window the agent can neither act nor see the human, and
`InputController` refuses to go on (`WalledOffError`), and pauses the task.

A window we may not read the integrity of is treated as outranking us: a medium
process is refused an elevated one's token, so "unreadable" is itself the sign.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass

from aegis_core.actuation import win32

#: Integrity-level RIDs (`winnt.h`). UIPI compares these.
MEDIUM_INTEGRITY = 0x2000
HIGH_INTEGRITY = 0x3000


@dataclass(frozen=True, slots=True)
class Blocker:
    """The focused window, when UIPI walls it off from us.

    `image` is the program's file name and nothing more: a window title can carry a
    document name or a path, and this ends up in logs and on the HUD.
    """

    #: 0 when no window has focus at all (a UAC prompt's secure desktop, the lock screen).
    pid: int
    #: `None` when Windows would not tell us — itself the sign that it outranks us.
    integrity: int | None
    image: str | None

    @property
    def no_foreground(self) -> bool:
        return self.pid == 0

    def describe(self) -> str:
        """For logs and test output: no title, no path."""
        if self.no_foreground:
            return "no window has focus (a secure desktop or the lock screen)"
        level = "unreadable" if self.integrity is None else f"{self.integrity:#06x}"
        return f"{self.image or 'unknown program'} (pid {self.pid}, integrity {level})"


@functools.cache
def own_integrity() -> int | None:
    """Ours, read once: a process's integrity level does not change while it runs."""
    return win32.process_integrity(os.getpid())


def outranks_us(theirs: int | None, ours: int | None) -> bool:
    """UIPI's rule: input may go to an equal or lower level, never a higher one."""
    if theirs is None or ours is None:
        return True
    return theirs > ours


def foreground_blocker() -> Blocker | None:
    """The focused window if it is walled off from us, else `None`."""
    pid = win32.foreground_pid()
    if pid is None:
        return Blocker(pid=0, integrity=None, image=None)
    theirs = win32.process_integrity(pid)
    if not outranks_us(theirs, own_integrity()):
        return None
    return Blocker(pid=pid, integrity=theirs, image=win32.process_image_name(pid))
