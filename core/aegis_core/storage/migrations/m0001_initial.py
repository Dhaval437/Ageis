"""The schema from `ARCHITECTURE.md § 7`.

Conventions every later migration should keep:

- Tables are `STRICT`, so a value of the wrong type is an error, not a coercion.
- Timestamps are UTC ISO-8601 `TEXT`, the same spelling as the event stream's `ts`.
- Booleans are `INTEGER` constrained to 0/1; `*_json` columns must hold valid JSON.
- Vocabularies the docs lock (task status, risk tier, Guardian decision, approval
  choice) are `CHECK`ed. Ones still open (step phase, observation kind) are not.
"""

from __future__ import annotations

from typing import Final

SQL: Final = """
CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    goal            TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN (
                        'QUEUED', 'RUNNING', 'PAUSED_BY_USER', 'WAITING_APPROVAL',
                        'DONE', 'FAILED', 'STOPPED', 'ABANDONED')),
    created_at      TEXT    NOT NULL,
    ended_at        TEXT,
    model_map_json  TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(model_map_json)),
    cost_cents      REAL    NOT NULL DEFAULT 0 CHECK (cost_cents >= 0),
    step_count      INTEGER NOT NULL DEFAULT 0 CHECK (step_count >= 0)
) STRICT;

-- RECOVERY.md § 3.2 scans for tasks left RUNNING on every start.
CREATE INDEX tasks_by_status ON tasks (status);

CREATE TABLE steps (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL CHECK (idx >= 0),
    phase           TEXT    NOT NULL,
    thought         TEXT,
    tool            TEXT,
    params_json     TEXT    CHECK (params_json IS NULL OR json_valid(params_json)),
    risk            TEXT    CHECK (risk IN ('SAFE', 'CAUTION', 'DANGEROUS', 'FORBIDDEN')),
    decision        TEXT    CHECK (decision IN ('allow', 'confirm', 'deny')),
    approved_by     TEXT,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    ok              INTEGER CHECK (ok IN (0, 1)),
    error           TEXT,
    observation_ref INTEGER REFERENCES observations (id) ON DELETE SET NULL,
    UNIQUE (task_id, idx)
) STRICT;

-- Images live on disk (%LOCALAPPDATA%\\Aegis\\obs); only the path is stored.
CREATE TABLE observations (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    step_id         INTEGER REFERENCES steps (id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL,
    path            TEXT,
    phash           TEXT,
    meta_json       TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(meta_json))
) STRICT;

CREATE INDEX observations_by_task ON observations (task_id);
CREATE INDEX observations_by_step ON observations (step_id);

-- `choice` and `decided_at` stay NULL while the approval is pending. A timeout
-- is recorded as 'deny' (REMEMBER.md invariant 6).
CREATE TABLE approvals (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES steps (id) ON DELETE CASCADE,
    prompt          TEXT    NOT NULL,
    choice          TEXT    CHECK (choice IN ('allow', 'deny', 'allow_always')),
    remembered      INTEGER NOT NULL DEFAULT 0 CHECK (remembered IN (0, 1)),
    decided_at      TEXT,
    CHECK ((choice IS NULL) = (decided_at IS NULL))
) STRICT;

CREATE INDEX approvals_by_step ON approvals (step_id);

-- Hash-chained and append-only. The chain is what makes tampering *evident*
-- (P6-04); the triggers stop the app itself from rewriting history by mistake.
CREATE TABLE audit (
    id              INTEGER PRIMARY KEY,
    ts              TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    event           TEXT    NOT NULL,
    payload_json    TEXT    NOT NULL CHECK (json_valid(payload_json)),
    prev_hash       TEXT    NOT NULL CHECK (length(prev_hash) = 64),
    hash            TEXT    NOT NULL UNIQUE CHECK (length(hash) = 64)
) STRICT;

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TABLE facts (
    id              INTEGER PRIMARY KEY,
    scope           TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    value           TEXT    NOT NULL,
    source_task_id  INTEGER REFERENCES tasks (id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL,
    UNIQUE (scope, key)
) STRICT;

-- The write-ahead undo record (RECOVERY.md § 2.1): written with applied = 0
-- before the operation runs, flipped to 1 after.
CREATE TABLE journal (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    step_id         INTEGER REFERENCES steps (id) ON DELETE SET NULL,
    op              TEXT    NOT NULL,
    forward_json    TEXT    NOT NULL CHECK (json_valid(forward_json)),
    undo_json       TEXT    CHECK (undo_json IS NULL OR json_valid(undo_json)),
    applied         INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    created_at      TEXT    NOT NULL,
    undone_at       TEXT
) STRICT;

CREATE INDEX journal_by_task ON journal (task_id, id);

CREATE TABLE settings (
    key             TEXT    PRIMARY KEY,
    value_json      TEXT    NOT NULL CHECK (json_valid(value_json))
) STRICT;
"""
