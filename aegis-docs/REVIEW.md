# REVIEW.md

> Quality gates for AEGIS. **No task in `PROGRESS.md` moves to `DONE` until the relevant checklist here passes.**
> If you are an agent finishing a task: run §1, then the checklist for your area, then the security gate in §5 if your task is marked *Security-gated*.

---

## 1. Universal Definition of Done

A task is done when **all** of these are true:

- [ ] It does what the task line in `PROGRESS.md` says — no more (no drive-by refactors), no less.
- [ ] Types check: `mypy --strict` clean for touched Python, `tsc --noEmit` clean for touched TS.
- [ ] Lint clean: `ruff check`, `eslint` — zero new warnings.
- [ ] Tests added for the new behaviour, and the whole suite passes locally.
- [ ] No `TODO`, `FIXME`, commented-out code, or debug `print`/`console.log` left behind.
- [ ] No secret, key, path from the developer's machine, or personal data in the diff.
- [ ] It works after a **restart of the app**, not just in a hot-reloaded dev session.
- [ ] `PROGRESS.md` updated (status + session log line).
- [ ] Anything architectural is recorded in `REMEMBER.md § Decision Log`.
- [ ] If it changed a screen, `UI.md` reflects it.
- [ ] If it changed an API shape, `ARCHITECTURE.md § 9` reflects it and the generated TS types were regenerated.

**Automatic reject:** a change that touches Guardian, the FORBIDDEN list, the key vault, redaction, the preload surface, or the kill switch **without** a corresponding test.

---

## 2. Code review checklist (any change)

**Correctness**
- [ ] Every error path is handled — no bare `except:`, no swallowed promises, no `.catch(() => {})`.
- [ ] Every external call (model API, OS API, subprocess) has a timeout.
- [ ] Async code checks the cancellation event at every await boundary in a long operation.
- [ ] Resources are released on every path (`try/finally`, context managers, `useEffect` cleanup).
- [ ] Concurrency: no shared mutable state across threads without a lock; the input-hook thread does *no* I/O.

**Fit with the system**
- [ ] New tool → registered with name, schema, risk tier, `undo()` or `undoable = False`, and `describe()`.
- [ ] New event type → added to `ARCHITECTURE.md § 9.2` and handled by the renderer store.
- [ ] New API route → added to §9.1, bearer-authed, Pydantic-validated request *and* response.
- [ ] New setting → has a default, migrates safely from an older DB, and appears in the Settings screen.
- [ ] Nothing new added to the preload surface without a §5 review.

**Robustness on a real user's machine**
- [ ] Works at 125% / 150% / 200% display scaling.
- [ ] Works on a second monitor and with a monitor unplugged mid-task.
- [ ] Works when the target window is minimised, occluded, or on another virtual desktop (or fails with a clear message).
- [ ] Handles paths with spaces, unicode, and long (>260 char) paths.
- [ ] Handles the machine locking / sleeping mid-task (agent pauses, does not blindly continue).
- [ ] Degrades sanely with no internet (local model still works; cloud calls fail with a clear message).

**Performance**
- [ ] No new work on the UI thread longer than 16 ms.
- [ ] No image larger than the configured max sent to a model.
- [ ] No unbounded list, log, or in-memory buffer.

---

## 3. UI review checklist

- [ ] Matches `UI.md` — tokens, spacing, radii, motion durations. No hard-coded hex colours.
- [ ] All four states exist: **default, loading, empty, error**. Storybook stories for each.
- [ ] Longest-realistic-content story exists (long file paths, long thoughts, 40-step timelines) and does not break layout.
- [ ] Keyboard: reachable, correct tab order, visible focus ring, `Esc` closes/denies.
- [ ] Screen reader: labelled controls, correct live-region politeness.
- [ ] Contrast checked against both themes.
- [ ] Risk communicated by **label + icon + colour**, never colour alone.
- [ ] Copy follows `UI.md § 11` — plain, second person, no softened risk words, no "Oops".
- [ ] Reduced-motion path tested.
- [ ] Nothing in the UI can trigger a destructive action in fewer clicks than the approval flow requires.

---

## 4. Agent-quality review (P4/P5 work)

Run the benchmark suite (`P4-12`) before calling any loop change done.

- [ ] ≥ 8/10 benchmark tasks pass unattended under Standard autonomy.
- [ ] No task exceeds its step budget without stopping cleanly.
- [ ] The stuck detector fires on a deliberately unwinnable task instead of looping.
- [ ] Every completed task's timeline is replayable and each step's claim matches its screenshot.
- [ ] The agent asks for help rather than guessing when grounding confidence is low.
- [ ] Cost per benchmark task is within the recorded baseline ±25% (regressions in cost are regressions).
- [ ] A failed step produces a *useful* error in the timeline, not a stack trace.

**Anti-patterns that fail review outright**
- Batching several actions from one model turn.
- Reporting success without a verification observation.
- Retrying the identical action more than twice.
- Sending full-resolution screenshots, or more than one image per model call.
- Letting the model output raw coordinates as the primary click path.

---

## 5. Security gate (mandatory for security-gated tasks)

Security-gated tasks: `P1-07`, `P2-05`, `P3-04`, `P3-05`, `P3-06`, `P3-09`, `P3-10`, `P5-07`, `P5-08`, `P5-11`, `P5-13`, `P6-04`, `P8-02`, and **any** change to the preload surface, the Guardian, or the installer.

**Boundary**
- [ ] The change does not widen what the agent can reach. If it does, that widening is explicit, user-visible, and revocable.
- [ ] Path handling normalises: `realpath`, symlink resolution, `..`, UNC paths, alternate data streams, 8.3 short names. Tested with adversarial inputs.
- [ ] Scope check happens on the **resolved** path, immediately before the operation, not at plan time.
- [ ] The FORBIDDEN list is still compiled in and still unreachable from the UI.

**Secrets**
- [ ] No key, token, or password is written to a log, an error message, an event payload, argv, an env var passed to a child, or the renderer.
- [ ] Redaction runs before *any* image or tree leaves the process, and there is a test that fails if the call is removed.
- [ ] Child processes get a scrubbed environment.

**Control**
- [ ] Kill switch still stops everything with the core hung (test by SIGSTOP-ing the core).
- [ ] Preemption still fires < 100 ms and releases every held modifier.
- [ ] No code path can run a DANGEROUS tool without a recorded approval row.
- [ ] Approval timeout still denies.

**Trust the model as little as possible**
- [ ] Tool params are schema-validated after the model returns them, before Guardian, before execution.
- [ ] Observation text is framed as untrusted data in the prompt and cannot become an instruction.
- [ ] A tool call whose params echo observation text not present in the user's goal escalates to `confirm`.

**Audit**
- [ ] The action is written to the hash-chained log with enough detail to reconstruct it.
- [ ] The log write cannot be skipped by an early return or an exception path.

Sign-off line to paste in the PR: `SECURITY GATE: reviewed against REVIEW.md §5 — <initials>, <date>`

---

## 6. Test requirements by layer

| Layer | Required |
|---|---|
| Guardian / policy | Unit test per rule, including one **negative** test per FORBIDDEN entry proving it is denied |
| Scope resolution | Property/fuzz test over adversarial paths |
| Redaction | Golden-image test: a screenshot with a password field must be pixel-black in that region |
| Input layer | Signature-tagging test: synthetic events must not trigger the preemption hook |
| Preemption / kill switch | Timed integration test asserting the latency targets |
| Model adapters | Recorded-response tests (VCR-style); one live smoke test per provider, skipped without a key |
| Router | Fallback, budget breach, capability-gate tests |
| Tools | Happy path + out-of-scope denial + undo round-trip |
| Agent loop | Benchmark suite + stuck-detector + budget-exhaustion tests |
| Recovery | Kill-mid-task-and-restart test; audit tamper test |
| UI | Vitest for stores/logic, Storybook for states, Playwright E2E for the three critical flows |

**Three critical E2E flows that must never break:** (1) start a task and watch it complete, (2) approve/deny a dangerous action, (3) preempt and resume.

---

## 7. Release gate (before any build reaches a user)

- [ ] All P8 gates green.
- [ ] Clean-VM install test passed on Windows 10 22H2 **and** Windows 11.
- [ ] Everything signed; SmartScreen shows no warning.
- [ ] Auto-update tested from the previous version.
- [ ] Uninstall tested, including the keep-or-delete-data path.
- [ ] `pip-audit` and `pnpm audit` clean of high/critical.
- [ ] Privacy statement matches actual behaviour, line by line.
- [ ] Version bumped, changelog written, git tag pushed.
- [ ] A rollback build is published and known-good.

---

## 8. How an agent should self-review before ending a session

1. Re-read the task line in `PROGRESS.md`. Did you do exactly that?
2. Run the full test suite. Not a subset.
3. Launch the built app (not dev mode) and exercise the feature by hand once.
4. Walk §1, then your area's checklist, then §5 if applicable.
5. Update `PROGRESS.md` and the session log.
6. If anything is unfinished, mark it `BLOCKED` or `TODO` **with a note explaining exactly where you stopped** — a half-finished task silently marked `DONE` costs the next session more than it saved you.
