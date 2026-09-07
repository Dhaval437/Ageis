# KICKOFF_PROMPT.md

How to actually start the build with Claude Code.

---

## Step 0 — Set up the folder (do this yourself, 2 minutes)

```powershell
mkdir D:\projects\aegis
cd D:\projects\aegis
git init
mkdir docs
# copy REMEMBER.md ARCHITECTURE.md UI.md PROGRESS.md REVIEW.md RECOVERY.md into docs\
```

Create `CLAUDE.md` in the repo root with exactly this:

```markdown
# AEGIS

Before doing ANYTHING in this repository, read `docs/REMEMBER.md` in full.
Then read `docs/PROGRESS.md` section 1 and the last row of section 12.

- `docs/REMEMBER.md` — identity, invariants, decisions. Read first, every session.
- `docs/ARCHITECTURE.md` — stack, processes, modules, APIs, security design.
- `docs/UI.md` — screens, components, states, copy rules.
- `docs/PROGRESS.md` — the task tracker. Pick work here; record work here.
- `docs/REVIEW.md` — definition of done and the security gate.
- `docs/RECOVERY.md` — undo, crash handling, dev-session recovery.

Rules that override anything else:
1. Work on exactly ONE task ID from PROGRESS.md at a time.
2. Never mark a task DONE without passing the matching checklist in REVIEW.md.
3. Never break an invariant from REMEMBER.md section 3. Not even temporarily.
4. Update PROGRESS.md (status + session log) before ending any session.
5. Commit messages start with the task ID: `P0-01: monorepo scaffold`.
6. If a design question isn't answered in the docs, pick the more conservative
   and more reversible option, implement it, and record it in the Decision Log.
```

Commit that: `git add . && git commit -m "docs: project plan"`

---

## Step 1 — The first session prompt

Paste this into Claude Code inside `D:\projects\aegis`:

```
Read docs/REMEMBER.md, then docs/ARCHITECTURE.md sections 2, 3 and 4,
then docs/PROGRESS.md section 2.

You are starting AEGIS from an empty repository. This session's job is
task P0-01 ONLY: the monorepo scaffold.

Build exactly the directory structure in ARCHITECTURE.md section 4, with:
- pnpm workspaces + turbo, root package.json, pnpm-workspace.yaml
- a shared tsconfig base and per-package tsconfigs
- apps/desktop (Electron), apps/renderer (Vite+React+TS+Tailwind),
  packages/shared, packages/ui, core/ (Python, pyproject.toml with
  ruff + mypy strict + pytest)
- eslint + prettier + ruff + mypy configs, .editorconfig, .gitignore
- placeholder entry files only, no feature code
- `pnpm install && pnpm build` must succeed, and `pytest` must run
  (with zero tests) without error

Do NOT start P0-02. When you're done:
1. verify the build and test commands actually run,
2. set P0-01 to DONE in docs/PROGRESS.md and append a session log row,
3. commit as "P0-01: monorepo scaffold".

Then stop and tell me what's next.
```

---

## Step 2 — Every session after that

The prompt is short, because the docs carry the context:

```
Read docs/REMEMBER.md and docs/PROGRESS.md.
Pick up the next TODO task whose blockers are all DONE, and do only that one.
Follow REVIEW.md before marking it DONE, and update PROGRESS.md before you stop.
```

If you want a specific task:

```
Read docs/REMEMBER.md and docs/PROGRESS.md.
Work on P3-05 (preemption) only. It is security-gated, so REVIEW.md section 5
applies. Show me the latency measurement before you mark it DONE.
```

---

## Step 3 — Sessions that need extra care

Some tasks deserve a longer prompt. Use these.

**P3-04/P3-05 (hooks + preemption) — the hardest low-level code in the project**

```
Read docs/REMEMBER.md, docs/ARCHITECTURE.md section 8.3, and docs/UI.md section 7.

Task P3-04 and P3-05. Install WH_MOUSE_LL and WH_KEYBOARD_LL hooks via ctypes
in a dedicated thread that does no I/O. Our own synthetic events are tagged with
dwExtraInfo = AEGIS_SIGNATURE and must be ignored by the hook.

Critical requirements:
- physical input sets the preempt event in under 5 ms inside the callback
- the in-flight input action aborts mid-sequence
- EVERY held modifier (Ctrl/Alt/Shift/Win, both sides) gets a KEYUP on abort
- write a timed integration test asserting < 100 ms end to end
- write a test proving synthetic events do NOT trigger preemption

A stuck modifier key after an abort is a P0 bug. Test for it explicitly.
```

**P4-02 (the agent loop) — the heart of the product**

```
Read docs/REMEMBER.md, docs/ARCHITECTURE.md section 6, and docs/REVIEW.md section 4.

Task P4-02: the agent loop. Enforce these, and add a test for each:
- exactly one tool call per model turn (reject a model response with more)
- every action followed by a verification observation
- a step budget that stops cleanly instead of looping
- cancellation checked at every phase boundary
- Guardian sits between the model's proposed action and execution, always

Keep prompts in versioned files under core/aegis_core/agent/prompts/,
never as inline strings.
```

**P7-01/P7-02 (packaging) — where solo projects usually stall**

```
Read docs/REMEMBER.md and docs/ARCHITECTURE.md section 11.

Tasks P7-01 and P7-02. PyInstaller onedir (NOT onefile) for the Python core into
apps/desktop/resources/core/, then electron-builder NSIS, per-user install,
no admin prompt.

Then actually test it: build the installer, and tell me the exact steps to verify
it on a clean Windows VM. List every hidden import you had to add and every file
electron-builder missed, in the session log — that list is what makes the next
build reproducible.
```

---

## Step 4 — What to do when a session goes sideways

| Symptom | Prompt |
|---|---|
| Claude Code starts refactoring things you didn't ask about | `Stop. Revert anything outside task <ID>. Re-read CLAUDE.md rule 1.` |
| It marks something DONE that isn't | `Re-read REVIEW.md section 1. Walk the checklist out loud against your change, then set the correct status.` |
| Build is broken and you don't know why | `Read docs/RECOVERY.md section 6.3 and follow it.` |
| You come back after a week | `Read docs/RECOVERY.md section 6.1 and follow it, then tell me where the project actually stands.` |
| It wants to add a dependency | `Justify it against ARCHITECTURE.md section 2. If it's a new core dependency, add a Decision Log row first.` |

---

## Step 5 — The one thing to do outside Claude Code, this week

Buy a **code signing certificate** (OV is fine to start; EV clears SmartScreen faster) and start the antivirus false-positive submission process (`P7-09`). A tool that synthesises keystrokes and reads the screen *will* be flagged. Code takes hours; certificate issuance and AV whitelisting take weeks. It is the only part of this project you cannot parallelise later.
