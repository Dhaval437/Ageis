"""Numbered SQL migrations, applied in order by `storage.db.migrate`.

The list is written out by hand rather than discovered from the directory. A
frozen PyInstaller build has no directory to scan, so a discovered migration
would silently go missing from the packaged app while every dev run passed.

Adding one: create `mNNNN_<name>.py` with an `SQL` constant, append it here with
the next version number, and never edit a migration that has shipped — a user's
database has already run it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from aegis_core.storage.migrations import m0001_initial


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema step. `version` becomes the database's `PRAGMA user_version`."""

    version: int
    name: str
    sql: str


MIGRATIONS: Final[tuple[Migration, ...]] = (Migration(1, "initial", m0001_initial.SQL),)
