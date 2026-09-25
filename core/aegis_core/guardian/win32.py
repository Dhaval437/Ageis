"""The Guardian's Win32 calls, declared in one place (the same rule as
`actuation/win32.py` and `perception/win32.py`: a separate `WinDLL`, so a prototype
set here cannot change one the input or capture layers rely on).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final

#: `GetDriveTypeW`'s answer for a drive mapped to a network share.
DRIVE_REMOTE: Final = 4

#: `st_file_attributes` bit for a reparse point, and the `st_reparse_tag` bit that
#: marks one as a *name surrogate* — a link to somewhere else (symlink, junction,
#: mount point) rather than a filter such as a cloud placeholder.
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
REPARSE_TAG_NAME_SURROGATE: Final = 0x20000000

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetDriveTypeW = _kernel32.GetDriveTypeW
    _GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _GetDriveTypeW.restype = wintypes.UINT


def drive_type(root: str) -> int:
    r"""`GetDriveTypeW(root)`, e.g. `C:\`. Asks the mount manager; no network I/O."""
    if sys.platform != "win32":
        return 0
    return int(_GetDriveTypeW(root))
