"""Named scopes (`P3-10`, `ARCHITECTURE.md § 8.2`).

A scope is a user's own definition — *Work Files*, *Downloads* — reused across tasks,
so it outlives any one of them and gets its own table rather than a `settings` key.
Folders and apps are JSON lists: a scope is always read whole, and a folder is only
ever compared after `guardian/scope.py` has canonicalised it again.

- **`name` is unique ignoring case**, because *Work files* and *Work Files* in one
  dropdown are one scope to a person.
- **Nothing here is trusted on the way back in.** `guardian/scope.load_scope()`
  re-validates every stored folder against the current FORBIDDEN list, so a row written
  by an older build, or edited by hand, cannot widen what a task may touch.
"""

from __future__ import annotations

from typing import Final

SQL: Final = """
CREATE TABLE scopes (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE COLLATE NOCASE
                            CHECK (length(name) BETWEEN 1 AND 64),
    folders_json    TEXT    NOT NULL CHECK (json_valid(folders_json)
                                            AND json_type(folders_json) = 'array'),
    apps_json       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(apps_json)
                                            AND json_type(apps_json) = 'array'),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
) STRICT;
"""
