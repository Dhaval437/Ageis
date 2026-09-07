# RECOVERY.md

> What happens when things go wrong — for the **user's data**, for the **running app**, and for the **development session**.
> Design rule behind every line here: *an agent that operates a computer will eventually do something wrong. The product's job is to make that survivable.*

---

## 1. The recovery promise (state it in the product, then honour it in code)

1. **Nothing the agent does is permanently destructive by default.** Deletes go to a shadow store, overwrites keep the previous version, moves are recorded with their origin.
2. **Every action is reversible or clearly labelled irreversible before it happens.** The approval dialog's reversibility banner (`UI.md § 5`) must never lie.
3. **The user can always see what happened**, even after a crash, a power cut, or a force-quit.
4. **A crash never leaves the machine in a broken state** — no held modifier keys, no half-written files, no orphan processes clicking away.

---

## 2. Undo: the action journal

### 2.1 Write-ahead, always

Before a mutating tool executes, `recovery/journal.py` writes a row:

```json
{
  "id": 481,
  "task_id": 12, "step_id": 97,
  "op": "fs.move",
  "forward": {"src": "D:\\inv\\a.pdf", "dst": "D:\\inv\\2024\\a.pdf"},
  "undo":    {"op": "fs.move", "src": "D:\\inv\\2024\\a.pdf", "dst": "D:\\inv\\a.pdf"},
  "applied": false,
  "created_at": "..."
}
```

Then the tool runs. Then `applied = true`. **Journal first, act second** — a crash between the two leaves a recorded intent that may not have happened, which is recoverable; the reverse leaves an action nobody knows about, which is not.

### 2.2 Undo payloads by operation

| Operation | Undo strategy |
|---|---|
| `fs.write_file` (existing file) | Copy the original to the shadow store first; undo = restore it |
| `fs.write_file` (new file) | Undo = delete the created file |
| `fs.move` / `fs.rename` | Undo = move back (verify the destination is still what we put there) |
| `fs.copy` | Undo = delete the copy |
| `fs.delete` | **Never a real delete.** Move to `%LOCALAPPDATA%\Aegis\trash\<task>\<uuid>\` preserving relative path; undo = move back |
| `fs.mkdir` | Undo = remove if still empty |
| `clipboard.write` | Save previous clipboard content; undo = restore |
| `input.type_text` into a field | Best-effort: record the field's prior value from the UIA tree; undo = re-select-all and retype the old value. **Marked "best effort", never "guaranteed"** |
| `window.close` | Not undoable → `undoable = False`, and the approval says so |
| `app.launch` | Undo = close the process we started (only that PID) |
| `shell.run_powershell` | **Not undoable.** Always DANGEROUS, always approved, always preceded by a restore point if the scope is large |
| `browser.fill_web` / form submit | Not undoable once submitted → the approval must say "this cannot be undone" |

### 2.3 Compound undo

`undo_last_n(task_id, n)` replays journal rows in **reverse order**, stopping at the first failure and reporting exactly which rows were reversed and which were not. It never silently partially-succeeds. The UI shows: *"Reversed 4 of 6 actions. Step 3 couldn't be undone because the file has changed since."*

### 2.4 Shadow store hygiene

`%LOCALAPPDATA%\Aegis\trash\` — retention default 14 days, size cap default 2 GB (both in Settings). Eviction is oldest-first, and a task's shadow entries are never evicted while that task is less than 24 hours old. Emptying it is a deliberate user action with a count-and-size confirmation.

### 2.5 System restore points

Before a batch classified as high-risk (any `shell` call, or > 20 file mutations in one task), `recovery/snapshot.py` attempts a VSS restore point via `Checkpoint-Computer`. Best-effort and **non-blocking**: if it fails or is throttled by Windows (it allows one per 24 h by default), log it and continue — but say so in the approval dialog: *"A system restore point could not be created."*

---

## 3. Crash and interruption recovery

### 3.1 Task states

`QUEUED → RUNNING → (PAUSED_BY_USER | WAITING_APPROVAL) → RUNNING → (DONE | FAILED | STOPPED | ABANDONED)`

State transitions are committed to SQLite **before** the corresponding side effect. A row left as `RUNNING` at startup means the process died mid-task.

### 3.2 On startup, for every task still marked `RUNNING`

1. Mark it `ABANDONED` with `ended_at = now`.
2. Scan its journal for rows with `applied = false` → these are *uncertain* actions. Verify each against the filesystem (does the destination exist? does the source still exist?) and classify: **did not happen**, **happened**, or **unknown**.
3. Release any input state (defensive: send `KEYUP` for all modifiers — cheap, and prevents the classic stuck-Ctrl-after-crash bug).
4. Kill any orphan job processes recorded in the task's child-PID list.
5. Show the **Recovered Task card** on the Home screen:

```
┌ Aegis stopped unexpectedly ────────────────────────────┐
│ Task "Organise invoices" was interrupted at step 14.    │
│                                                        │
│ 13 actions completed · 1 uncertain                     │
│   ? fs.move  a.pdf → 2024\   (can't confirm)           │
│                                                        │
│ [ Undo all 13 ]  [ Review steps ]  [ Dismiss ]         │
└────────────────────────────────────────────────────────┘
```

**Aegis never auto-resumes a task after a crash.** The screen may have changed completely; resuming blind is exactly how a small failure becomes a large one. The user re-issues the task if they want it.

### 3.3 Mid-task disruptions that are not crashes

| Event | Behaviour |
|---|---|
| Machine locks / screensaver | Pause immediately. Perception is meaningless on a lock screen and clicks would land unpredictably. Resume prompt on unlock. |
| Sleep / hibernate | Pause on the resume-from-sleep event, re-observe before continuing. |
| User logs out | Core exits cleanly, task → `ABANDONED`. |
| RDP / session switch | Treat as lock. |
| Display configuration change (monitor added/removed/resolution change) | Invalidate the coordinate cache, force a fresh observation, log it. Never reuse a pre-change bounding box. |
| Target window closed by the user | Current step fails with a clear error; the agent re-plans rather than clicking where the window used to be. |
| Network loss (cloud model) | Retry with backoff (3 attempts), then fall back per the router chain, then pause and tell the user. |
| Model returns garbage / unparseable tool call | Retry once with a repair prompt; second failure → fail the step and surface it. |

---

## 4. Engine (core process) failure

Detected by MAIN via health-check failure, broken pipe, or watchdog timeout.

1. MAIN kills the core and any child job processes.
2. Attempts a respawn (max 3 within 60 s).
3. On success: the UI reconnects to the event stream with `?since=<last_seq>` and replays from SQLite — the user sees a brief "Reconnecting…" and nothing else.
4. On 3 failures: the **Engine unavailable** screen:

```
┌────────────────────────────────────────────────────────┐
│  The Aegis engine isn't running                        │
│                                                        │
│  It stopped 3 times in a row. Your data is safe and    │
│  your task history is intact.                          │
│                                                        │
│  [ Restart engine ]  [ Open logs ]  [ Copy report ]    │
│                                                        │
│  Common causes: antivirus blocked aegis-core.exe,      │
│  or another program is using the automation APIs.      │
└────────────────────────────────────────────────────────┘
```

`Copy report` produces a redacted diagnostic bundle (versions, last 200 log lines, no paths from the user's home, no keys).

**Absolute rule:** if the core dies while a task is running, MAIN's very first action — before any UI update — is to release all modifier keys via its own input path. A dead agent must not leave the keyboard in a broken state.

---

## 5. Data recovery

| Asset | Protection |
|---|---|
| `aegis.db` | WAL mode; automatic backup copy on every app start (keep last 5); integrity check on open; corrupt DB → rename to `aegis.db.corrupt-<ts>`, start fresh, tell the user where the old one is |
| API keys | In Windows Credential Manager, independent of the DB — a DB reset never loses them |
| Screenshots | On disk, referenced by path; a missing file degrades the timeline gracefully ("screenshot unavailable"), it never crashes the view |
| Audit log | Hash-chained; a break is *reported*, never silently repaired |
| Settings | JSON blob in the DB **plus** a mirrored `settings.backup.json`; restore offered if the DB is reset |

**Export before destruction, always.** Anything in the app that resets or deletes user data first offers an export.

---

## 6. Development-session recovery (for agents building this)

This section is for a Claude Code session that gets interrupted, confused, or inherits a half-finished state.

### 6.1 Starting cold
1. Read `REMEMBER.md` in full. It is short and it is the map.
2. Read `PROGRESS.md § 1` and `§ 12` (session log). The last log line tells you where the previous session actually stopped — trust it over your own inference.
3. Run `pnpm install && pnpm build && pnpm test` before touching anything. If the tree is already broken, **fixing the build is your task**, regardless of what `PROGRESS.md` says is next.
4. Only then pick up a task.

### 6.2 Finding a task marked `WIP` from a previous session
Assume it is **incomplete and possibly wrong**. Do not build on it blindly:
1. Read the diff of the last commits touching that area.
2. Run its tests specifically.
3. Either finish it (and note in the session log that you inherited it) or revert it to a clean state and restart it. Do not leave a third partial layer on top of two others.

### 6.3 When the build is broken and you don't know why
1. `git stash` your changes, confirm `main` builds. If it doesn't, the breakage predates you — fix that first and say so in the session log.
2. Bisect by phase: core alone (`pytest`), then UI alone (`pnpm --filter renderer test`), then the handshake.
3. The three usual suspects, in order: stale generated types (`pnpm gen:types`), a stale PyInstaller build in `resources/core/`, a leftover core process holding the DB lock (`Get-Process aegis-core | Stop-Process`).

### 6.4 When you are unsure about a design decision
Do **not** invent one silently. In order:
1. Check `REMEMBER.md § Decision Log` — it may already be decided.
2. Check `ARCHITECTURE.md` — the answer is probably there.
3. If genuinely undecided: pick the option that is **more conservative about permissions and more reversible**, implement it, and record it in the Decision Log with the reasoning and the alternative you rejected. A recorded decision you can revisit beats an unrecorded one you can't find.

### 6.5 Rules for never losing work
- Commit at every green test, with a message naming the task ID: `P3-05: preemption releases held modifiers`.
- Never leave the session without updating `PROGRESS.md`. This is the recovery mechanism for the whole project.
- Never `git push --force` to `main`.
- If you must stop mid-task, commit to a branch `wip/P3-05` and write in the session log **exactly** what is done, what is not, and what you would do next.

---

## 7. Incident playbook (a user reports the agent did something bad)

1. **Get the task report** — `History → task → Export report`. It contains the full step list, screenshots, approvals, and the audit slice.
2. **Verify the audit chain.** If it fails, the machine is compromised or the DB was edited; investigate that first.
3. **Locate the action** in the journal. Was it approved? By whom, and what did the dialog say?
4. **Classify:**
   - *Guardian gap* — the action should have been `DANGEROUS`/`FORBIDDEN` and wasn't → add the rule, add a test, patch release. Highest severity.
   - *Approval-copy failure* — the user approved but the dialog didn't convey the consequence → fix the `describe()` string, add it to the UI copy tests.
   - *Grounding failure* — right intent, wrong element clicked → benchmark case + verification improvement.
   - *Model failure* — bad plan → prompt/planner work, and check whether verification should have caught it.
   - *Prompt injection* — content on screen redirected the agent → treat as a security incident, §8.6 row 1 of `ARCHITECTURE.md`.
5. **Recover the user's data** using the journal and shadow store; walk them through it rather than doing it silently.
6. **Write it up** in `REMEMBER.md § Incidents` with the root cause and the test that now prevents it. Every incident must leave behind a test.
