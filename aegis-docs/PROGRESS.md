# PROGRESS.md

> **The single source of truth for what is done, what is next, and who is doing it.**
> Every agent session **must** update this file before it ends. An unrecorded change is a lost change.
>
> Task IDs are permanent. Never renumber. New work gets the next free number in its phase (e.g. `P3-11`).

---

## 0. How to use this file

**At the start of a session**
1. Read `REMEMBER.md`, then this file's §1 and §2.
2. Pick the topmost `TODO` task whose blockers are all `DONE`.
3. Set it to `WIP` with your session date. Only **one** task may be `WIP` at a time per developer.

**At the end of a session (mandatory)**
1. Set the task to `DONE`, `BLOCKED`, or back to `TODO` with a note.
2. Append a line to §6 Session Log.
3. If you made an architectural decision, add it to `REMEMBER.md § Decision Log` — not here.
4. If you discovered new work, add it as a new task with the next free ID.

**Status values:** `TODO` · `WIP` · `BLOCKED` · `REVIEW` · `DONE` · `CUT`
A task is only `DONE` when it passes the corresponding gate in `REVIEW.md`.

---

## 1. Snapshot

| | |
|---|---|
| **Current phase** | P0 — Foundations |
| **Current task** | P0-06 — Startup handshake (next) |
| **Last session** | 2026-09-09 — P0-05 Python core skeleton |
| **Overall** | 7 / 100 tasks |
| **Ship target for v1** | Windows installer, Standard autonomy, fs + input + shell + browser tools |

### Phase progress

| Phase | Name | Tasks | Done | Status | Exit gate |
|---|---|---|---|---|---|
| P0 | Foundations & plumbing | 15 | 5 | 🟨 | App launches, core handshake works, one round-trip |
| P1 | Model layer | 10 | 0 | ⬜ | Chat with any of 3 providers; keys stored in DPAPI |
| P2 | Perception | 9 | 0 | ⬜ | Agent can describe the screen and list clickable elements |
| P3 | Actuation + safety spine | 14 | 2 | 🟨 | Agent clicks correctly; kill switch and preemption both < targets |
| P4 | The agent loop | 12 | 0 | ⬜ | 8 of 10 benchmark tasks complete unattended |
| P5 | Tool families | 13 | 0 | ⬜ | fs / shell / browser all behind Guardian |
| P6 | Recovery, undo, audit | 9 | 0 | ⬜ | Every destructive op is undoable; log chain verifies |
| P7 | Packaging & installer | 10 | 0 | ⬜ | Signed `Setup.exe` installs and auto-updates on a clean VM |
| P8 | Hardening & polish | 8 | 0 | ⬜ | Threat-model tests green; a11y pass; perf targets met |

---

## 2. P0 — Foundations & plumbing

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P0-01 | Monorepo scaffold: pnpm workspaces, turbo, tsconfig base, ruff/mypy/eslint/prettier configs, `.editorconfig` | — | DONE | 2026-09-07. Layout per `ARCHITECTURE.md § 4`; docs stayed in `aegis-docs/`. `pnpm build` + `pytest` green |
| P0-02 | Electron MAIN skeleton: window, custom titlebar, tray, single-instance lock | P0-01 | DONE | 2026-09-07. Frameless 1100×720 (min 880×600), tray with status/stop/show-hide/quit, single-instance lock verified. Renderer draws the titlebar in P0-03, so the window is currently drag-less; window controls need P0-04 |
| P0-03 | Renderer skeleton: Vite + React + Tailwind + shadcn, dark tokens from `UI.md § 2` | P0-01 | DONE | 2026-09-09. Both palettes wired (light derived — see Decision Log), stock Tailwind scales cleared so off-token classes don't compile, shadcn foundation (`components.json`, `cn`, `Button`), `AppShell` to the `UI.md § 4` layout. Vitest + RTL added; 35 renderer tests |
| P0-04 | Preload bridge with the exact 6-namespace surface | P0-02 | DONE | 2026-09-09. All six namespaces, one named channel per method, no generic `invoke`. Contract in `packages/shared/src/bridge.ts`; validation + policy in `main/bridge-handlers.ts` (electron-free); Electron wiring, sender check and disposal in `main/ipc.ts`. `system.openPath`/`revealInExplorer` gated by `main/path-grants.ts`. Unbuilt subsystems answer `unavailable`. Titlebar minimise/close now live |
| P0-05 | Python core skeleton: FastAPI app, `/v1/health`, structured logging to file + stdout | P0-01 | DONE | 2026-09-09. `server/app.py` (`create_app()`), `server/routes.py` (`/v1/health`), `server/schemas.py` (`HealthResponse`), `logging_setup.py` (JSON lines → rotating file + **stderr**, not stdout — see Decision Log), `__main__.py` serves it on loopback. Docs/OpenAPI off. Handshake, auth and peer-PID check are P0-06 |
| P0-06 | **Startup handshake**: ephemeral port, token over stdin pipe, stdout JSON line, peer-PID check | P0-02, P0-05 | TODO | The 6 steps in `ARCHITECTURE.md § 3.1`, all of them |
| P0-07 | Supervisor: spawn, health-check, 3-strike respawn, kill core when MAIN exits | P0-06 | TODO | Test: kill MAIN → core gone in ≤2 s |
| P0-08 | WebSocket event hub + `seq` replay from memory (SQLite replay lands in P6) | P0-05 | TODO | |
| P0-09 | Renderer event-stream client + Zustand store; UI is a pure function of the stream | P0-08, P0-03 | TODO | |
| P0-10 | SQLite bootstrap + migration runner + the schema from `ARCHITECTURE.md § 7` | P0-05 | TODO | WAL mode on |
| P0-11 | Pydantic→TS type generation script wired into `pnpm dev` | P0-05, P0-03 | TODO | CI fails if generated types are stale |
| P0-12 | Fix the root `clean` script: `rimraf node_modules` deletes the rimraf it is running from, so it always exits 1 half-done | — | TODO | Found during P0-02. Run it and you must `pnpm install` again |
| P0-13 | Storybook for the renderer, with the four required stories per component | P0-03 | TODO | Found during P0-03. `REVIEW.md § 3` demands default/loading/empty/error stories, and the `UI.md § 12` inventory starts landing at P3-12 — this must exist before it does |
| P0-14 | Bundle Inter + JetBrains Mono as local `woff2` and self-host them | P0-03 | TODO | Found during P0-03. `UI.md § 2` names both; the app must render correctly offline, so no webfont CDN — the CSP has no `font-src` beyond `'self'` for that reason |
| P0-15 | Maximise/restore: add `window.maximize` to the bridge surface, then wire the `□` button | P0-04 | TODO | Found during P0-04. The `UI.md § 4.1` sketch shows `– □ ×` but `ARCHITECTURE.md § 9.3` has only `minimize`/`close`, so the button was left out rather than the surface widened silently. Needs a `REVIEW.md § 5` sign-off and an `ARCHITECTURE.md § 9.3` edit |

**Gate:** typing in the composer sends a request to the core and a streamed echo renders in the timeline. Kill MAIN → no orphan process.

---

## 3. P1 — Model layer

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P1-01 | `ModelProvider` protocol + `ChatRequest`/`ChatDelta`/`Capabilities` schemas | P0-05 | TODO | |
| P1-02 | `openai` adapter (streaming, tool calls, vision, configurable base URL) | P1-01 | TODO | The workhorse — 5 providers reuse it |
| P1-03 | `anthropic` adapter | P1-01 | TODO | |
| P1-04 | `google` adapter | P1-01 | TODO | |
| P1-05 | `nvidia`, `openrouter`, `custom` as base-URL configs over `openai` | P1-02 | TODO | Do **not** fork the client |
| P1-06 | `ollama` adapter + auto-detect on `localhost:11434` | P1-01 | TODO | Privacy story; must work fully offline |
| P1-07 | Key vault on DPAPI via `keyring`; never argv/logs/renderer; masked display | P0-05 | TODO | Security-gated (`REVIEW.md § 5`) |
| P1-08 | Router: role map (Planner/Grounder/Utility), fallback chain, capability gate | P1-02..P1-06 | TODO | |
| P1-09 | Budget guard: per-task + per-day ceilings, live cost events | P1-08 | TODO | Pause on breach, never continue |
| P1-10 | Models screen (`UI.md § 8.4`): role cards, provider cards, Test button, spend meter | P1-08, P0-09 | TODO | |

**Gate:** user pastes a key for any supported provider, hits Test, sees a real completion; a killed primary falls back to the secondary; keys survive an app restart and never appear in any log.

---

## 4. P2 — Perception

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P2-01 | Per-monitor DPI awareness + a virtual-desktop coordinate model | P0-05 | TODO | Get this wrong and every click is wrong |
| P2-02 | `screen.capture()` via `mss`: monitor / window / region, WebP encode | P2-01 | TODO | < 150 ms |
| P2-03 | `uia_tree.walk()`: foreground window → element list with role/name/value/bbox | P2-01 | TODO | |
| P2-04 | Tree pruning + relevance ranking, cap 200 elements | P2-03 | TODO | Biggest quality lever in the whole app |
| P2-05 | **`redact.py`**: password fields + secrets-regex → black boxes in image bytes, values stripped from tree | P2-02, P2-03 | TODO | Security-gated. Test must fail the build if bypassed |
| P2-06 | Set-of-mark renderer: numbered overlays on candidate elements | P2-04, P2-02 | TODO | |
| P2-07 | OCR fallback (Windows OCR via winrt, Tesseract fallback) | P2-02 | TODO | For canvas apps with no tree |
| P2-08 | `grounding.resolve(element_id) -> point`, re-verified against a fresh capture | P2-04 | TODO | |
| P2-09 | Perceptual hash on observations (feeds the stuck detector) | P2-02 | TODO | |

**Gate:** on a live Explorer window the core returns a correct element list, a marked screenshot, and grounding for a named button — with a password field on screen fully black-boxed.

---

## 5. P3 — Actuation + safety spine

> **This is the phase that decides whether the product is trustworthy. Do not compress it.**

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P3-01 | `SendInput` wrapper: move, click, double, right, drag, scroll, type (unicode), key combos | P2-01 | WIP | Partly landed with P3-04: `actuation/win32.py` + `SendInputBackend` cover relative move, buttons, drag, scroll, unicode typing, combos. **Still owed:** absolute/DPI-aware `move_to` (needs P2-01) and the full VK table |
| P3-02 | Signature tagging: every synthetic event carries `dwExtraInfo = AEGIS_SIGNATURE` | P3-01 | DONE | 2026-09-09. `actuation/signature.py` + `SendInputBackend`, which is the only caller of `SendInput`. Landed early because P3-04 is meaningless without it |
| P3-03 | Human-like motion (bezier path, small jitter, sane dwell) | P3-01 | TODO | Improves reliability in apps with hover states |
| P3-04 | **Low-level hooks** `WH_MOUSE_LL` / `WH_KEYBOARD_LL`, ignoring signed events | P3-02 | DONE | 2026-09-09. `actuation/preempt.py`: dedicated thread, hooks + message loop, no I/O in the callback. Measured 1.06 ms median from injection to signal set (budget 5 ms) |
| P3-05 | **Preemption**: real input → abort in-flight action, release all held modifiers, PAUSED_BY_USER | P3-04 | DONE | 2026-09-09. Abort + full modifier release measured at **6.3 ms median / 9.3 ms worst** over 10 runs (budget 100 ms). `PAUSED_BY_USER` itself is the listener seam — the task state machine arrives with P4 |
| P3-06 | **Kill switch**: global hotkey in MAIN → hard stop, release keys, kill job children, freeze | P0-02, P3-05 | TODO | Must work when the core is hung |
| P3-07 | Watchdog: MAIN pings core 1/s; 2 misses while running → kill core | P0-07 | TODO | |
| P3-08 | Guardian core: `evaluate()`, tier model, `rules.yaml` loader | P0-05 | TODO | |
| P3-09 | FORBIDDEN list compiled in, not editable from UI, with unit tests per entry | P3-08 | TODO | Security-gated |
| P3-10 | Scope model: named scopes, path normalisation (realpath, symlinks, `..`), enforcement | P3-08 | TODO | Security-gated |
| P3-11 | Approvals: request/resolve API, timeout auto-**deny**, `allow_always` with scoping | P3-08, P0-08 | TODO | |
| P3-12 | `ApprovalDialog` component to `UI.md § 5` spec incl. 200 ms input guard and Deny-default focus | P3-11, P0-09 | TODO | |
| P3-13 | `OverlayHUD` window: click-through, cursor-avoidance, pause/stop, amber approval state | P0-02, P0-09 | TODO | |
| P3-14 | Preemption + kill-switch UX (`UI.md § 7`): edge flash, "You took over", Resume-with-a-note | P3-05, P3-06, P3-13 | TODO | |

**Gate:** with a task running, (a) moving the physical mouse pauses the agent in < 100 ms with no stuck keys, (b) the hotkey stops everything in < 200 ms even with the core deliberately hung, (c) a scripted attempt to write into `C:\Windows` is denied and logged, (d) an approval left untouched for 30 s denies itself.

---

## 6. P4 — The agent loop

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P4-01 | Tool registry: name, schema, risk, `undo()`, `describe()` — all five enforced by test | P3-08 | TODO | |
| P4-02 | Loop skeleton: perceive → plan → propose → guardian → execute → observe | P4-01, P2-*, P3-* | TODO | One action per turn, no batching |
| P4-03 | System prompt + tool-schema serialisation per provider | P1-08, P4-01 | TODO | Keep prompts in versioned files, not inline strings |
| P4-04 | Context assembly (`ARCHITECTURE.md § 6.2`) with `UTILITY`-model step compression | P1-08 | TODO | One image per call, max |
| P4-05 | Verification step: did the expected change occur? | P2-09, P4-02 | TODO | Unverifiable ≠ successful |
| P4-06 | Stuck detector (3 near-identical observations + unchanged plan) | P2-09, P4-02 | TODO | |
| P4-07 | Step + cost budgets, graceful stop-and-report | P1-09, P4-02 | TODO | |
| P4-08 | Cancellation/pause/resume plumbed through every phase and long action | P4-02 | TODO | |
| P4-09 | Mid-run instruction injection (`POST /tasks/{id}/message`) | P4-02 | TODO | Pairs with P3-14 |
| P4-10 | `ask_user`, `finish`, `give_up`, `remember_fact` task tools + the `facts` table | P4-01 | TODO | |
| P4-11 | `StepCard`, `TimelineList`, `LiveView` to `UI.md § 4.2/4.3` | P0-09 | TODO | Streaming thought text |
| P4-12 | **Benchmark suite**: 10 scripted real tasks with pass criteria, runnable headlessly | P4-02 | TODO | This is how we measure "is it actually good" |

**Gate:** ≥ 8/10 benchmark tasks complete without human help under Standard autonomy; every run has a full, replayable timeline.

---

## 7. P5 — Tool families

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P5-01 | `screen`: observe, wait_for, read_text | P2-* | TODO | |
| P5-02 | `input`: click, double, right, drag, type_text, press_keys, scroll | P3-01 | TODO | |
| P5-03 | `window`: list, focus, move_resize, close | P2-03 | TODO | |
| P5-04 | `app`: launch (allowlisted + user-approved), is_running | P3-08 | TODO | |
| P5-05 | `fs` read family: list_dir, read_file (text + pdf/docx extraction) | P3-10 | TODO | Scope-enforced |
| P5-06 | `fs` write family: write, move, copy, mkdir, zip/unzip | P3-10, P6-01 | TODO | Journalled before execution |
| P5-07 | `fs.delete` → shadow copy to Aegis trash, never a real delete | P6-02 | TODO | Security-gated |
| P5-08 | `shell.run_powershell` in a low-integrity child, scrubbed env, hard timeout, output cap | P3-08 | TODO | Security-gated. Always DANGEROUS |
| P5-09 | Command classifier: block the destructive-verb list before it reaches PowerShell | P5-08 | TODO | Defence in depth, not the only defence |
| P5-10 | `browser`: Playwright with a dedicated profile — open, goto, find, click, extract | P3-08 | TODO | Never reuse the user's real Chrome profile |
| P5-11 | `browser.fill_web` — DANGEROUS tier, never fills password/payment fields | P5-10 | TODO | Security-gated |
| P5-12 | `clipboard` read/write with redaction on read | P2-05 | TODO | |
| P5-13 | Egress allowlist enforcement for the whole core process | P3-08 | TODO | Security-gated |

**Gate:** each tool has params validation, a risk tier, an `undo()` or an explicit `undoable = False`, a `describe()` string a non-technical person understands, and a test proving Guardian blocks its out-of-scope form.

---

## 8. P6 — Recovery, undo, audit

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P6-01 | Action journal: write intent + undo payload **before** execution | P4-01 | TODO | |
| P6-02 | Shadow-copy store (`%LOCALAPPDATA%\Aegis\trash`) with retention + size cap | P6-01 | TODO | |
| P6-03 | `undo(journal_id)` and `undo_last_n` incl. compound file operations | P6-01, P6-02 | TODO | |
| P6-04 | Hash-chained audit log + `aegis verify-log` | P0-10 | TODO | Security-gated |
| P6-05 | Crash-safe task state: resume or cleanly abandon on restart | P0-10 | TODO | Per `RECOVERY.md § 3` |
| P6-06 | Event replay from SQLite on WS reconnect (`?since=seq`) | P0-08, P0-10 | TODO | |
| P6-07 | VSS restore point before high-risk batches (best-effort, non-blocking) | P5-06 | TODO | |
| P6-08 | Signed task report export (`.zip`: steps, screenshots, audit slice) | P6-04 | TODO | |
| P6-09 | Logs screen + `Verify log integrity` button (`UI.md § 8.5`) | P6-04, P0-09 | TODO | |

**Gate:** kill the app mid-task → restart shows the correct state and offers undo; a manually edited audit row makes verification fail.

---

## 9. P7 — Packaging & installer

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P7-01 | `build-core.ps1`: PyInstaller onedir, hidden imports resolved, size audit | P0-05 | TODO | onedir, **not** onefile |
| P7-02 | electron-builder config: NSIS, per-user, no admin, shortcuts, extraResources | P7-01 | TODO | |
| P7-03 | Code signing (`sign.ps1`) for exe, sidecar, installer | P7-02 | TODO | Blocks release |
| P7-04 | Auto-update via electron-updater + static feed + signature verification | P7-03 | TODO | |
| P7-05 | Clean-VM install test matrix: Win 10 22H2 + Win 11, fresh user, no Python, no Node | P7-02 | TODO | Must pass before every release |
| P7-06 | Uninstaller: remove app, offer keep-or-delete for user data | P7-02 | TODO | |
| P7-07 | First-run wizard (`UI.md § 8.7`) including the practise-the-kill-switch step | P3-14 | TODO | |
| P7-08 | Portable `.zip` build | P7-02 | TODO | |
| P7-09 | AV/SmartScreen false-positive submissions to Microsoft + major vendors | P7-03 | TODO | **Start this early — it takes weeks** |
| P7-10 | Licensing scaffold: Ed25519 offline verify, `features.enabled()` gates returning True | P0-05 | TODO | No billing in v1 |

**Gate:** a fresh Windows VM with nothing installed runs `Setup.exe`, completes the wizard, finishes a real task, and takes an auto-update — with no SmartScreen warning.

---

## 10. P8 — Hardening & polish

| ID | Task | Blocked by | Status | Notes |
|---|---|---|---|---|
| P8-01 | Prompt-injection test suite: screens/pages that try to hijack the agent | P4-02 | TODO | Row 1 of the threat model |
| P8-02 | Injection mitigation: observation text framed as untrusted; echoed params escalate to `confirm` | P8-01 | TODO | Security-gated |
| P8-03 | Full threat-model test pass (`ARCHITECTURE.md § 8.6`, one test per row) | P8-02 | TODO | |
| P8-04 | Performance pass against every target in `ARCHITECTURE.md § 13` | P7-05 | TODO | |
| P8-05 | Accessibility pass: keyboard-only run, contrast audit, screen-reader check | P4-11 | TODO | |
| P8-06 | Error-state pass: every row of `UI.md § 9` implemented and screenshot-tested | P4-11 | TODO | |
| P8-07 | Docs: README, user guide, security whitepaper (the sales asset), privacy statement | P7-05 | TODO | |
| P8-08 | Beta with 10 real users; triage log; ship-or-fix decision | P8-03..P8-07 | TODO | |

**Gate:** no open security-gated issue; all perf targets met; 10 beta users, zero data-loss incidents.

---

## 11. Parallelisation guide

If more than one agent/session is working at once, these tracks barely touch:

- **Track A (core/agent):** P1 → P2 → P4 → P5
- **Track B (safety):** P3-04 … P3-11 → P6 → P8-01..03
- **Track C (UI):** P0-03, P0-09 → P3-12/13/14 → P4-11 → P8-05/06
- **Track D (release):** P7-01..P7-09 — can start as soon as P0 is done; **start P7-09 in week one**

Shared files that need care: `tools/registry.py`, `server/schemas.py`, `packages/shared/src/api.ts`.

---

## 12. Session log

Append one line per session. Newest at the bottom.

```
| Date | Session/agent | Tasks touched | Outcome | Next |
|------|---------------|---------------|---------|------|
```

| Date | Session | Tasks touched | Outcome | Next |
|---|---|---|---|---|
| 2026-09-05 | planning | — | Doc set created (ARCHITECTURE, UI, PROGRESS, REVIEW, RECOVERY, REMEMBER) | Start P0-01 |
| 2026-09-07 | claude-code | P0-01 | Monorepo scaffold: pnpm workspaces + turbo, tsconfig base, apps/desktop (Electron), apps/renderer (Vite+React+Tailwind v4), packages/shared + ui, core/ (ruff + mypy strict + pytest). `pnpm install && pnpm build`, eslint, tsc, ruff, mypy, pytest all green | P0-02 Electron MAIN skeleton |
| 2026-09-07 | claude-code | P0-02, P0-12 | MAIN skeleton: frameless 1100×720 window, navigation locked down, tray (status / stop / show-hide / quit), single-instance lock. Preload moved to `bridge.cts` so it emits CJS. Vitest added — `pnpm test` ran **zero** tests before and now runs 10 TS + 2 pytest. Launched by hand: one window, second instance exits without spawning, close → 0 orphan processes. Logged P0-12 for the broken root `clean` | P0-03 renderer skeleton |
| 2026-09-09 | claude-code | P0-03, P0-13, P0-14 | Renderer skeleton: both `UI.md § 2` palettes as explicit blocks (dark default, OS picks light, `data-theme` overrides) with the light values derived and contrast-checked; Tailwind's stock colour/size/radius scales cleared so an off-token class no longer compiles; shadcn foundation (`components.json`, `cn` over `extendTailwindMerge`, `Button`); `AppShell` to the `UI.md § 4` layout — 44px titlebar with a drag region, 64px rail, 360px live view, full-width composer disabled with "Connect a model to get started." Vitest + RTL wired into `apps/renderer` (35 tests, incl. one that fails if a token is dropped or defined for only one theme). Built app launched by hand: layout matches the sketch, rail switches sections, close → 0 orphan processes. Logged P0-13 (Storybook) and P0-14 (bundle the fonts) | P0-04 preload bridge |
| 2026-09-09 | claude-code | P3-04, P3-05, P3-02, part of P3-01 | Preemption spine. `actuation/win32.py` (ctypes bindings), `signature.py` (`AEGIS_SIGNATURE`), `input.py` (`SendInputBackend` — the only `SendInput` caller — plus `InputController` with a held-key registry and abort-aware pacing), `preempt.py` (`PreemptSignal` + `InputMonitor`: both LL hooks on one thread that does no I/O). 30 new tests. Measured on this machine: hook callback sets the signal in **1.06 ms median / 3.5 ms worst** (n=25); real keystroke → action aborted → all eight modifiers released in **6.3 ms median / 9.3 ms worst** (n=10). Suite run 5× consecutively, no flakes | P3-06 kill switch |
| 2026-09-09 | claude-code | P0-04, P0-15 | Preload bridge. `packages/shared/src/bridge.ts` holds the `AegisBridge` contract (all six `ARCHITECTURE.md § 9.3` namespaces); `preload/bridge.cts` exposes it over one named channel per method with no generic `invoke`; `main/bridge-channels.ts` lists every channel; `main/bridge-handlers.ts` is electron-free and does all validation; `main/ipc.ts` is the only file touching `ipcMain`/`dialog`/`shell` and adds a trusted-sender check plus `dispose()`. Calls resolve a `BridgeResult` and never reject, so `unavailable` (core/hotkeys/updates/overlay not built yet) is distinguishable from `failed`. `system.openPath`/`revealInExplorer` are gated by `main/path-grants.ts`: only paths that *resolve* (realpath — junctions, 8.3 names, `..`) inside a user-picked folder or an Aegis data dir, and `openPath` refuses executables outright. Titlebar minimise/close wired. 39 new tests (67 desktop, 37 renderer, 32 pytest, all green). Verified in real Electron with a throwaway harness against the compiled preload: exactly six namespaces with exactly the documented members, `C:\Windows\System32\cmd.exe` denied `not_granted`, `http://evil/` denied `invalid_request`, logs path revealed OK, minimise and close both working. Two notes for the next session — `apps/desktop` had to drop `verbatimModuleSyntax` (Decision Log) and **`ELECTRON_RUN_AS_NODE=1` in the agent terminal stops Electron launching at all**; see the new `RECOVERY.md § 6.3.1`. Logged P0-15 (maximise) | P0-05 Python core skeleton |
| 2026-09-09 | claude-code | P0-05 | Python core skeleton. `server/app.py` is a `create_app()` factory (no module-level singleton, so every test gets its own uptime clock) with a lifespan that logs `core.started`/`core.stopped`; `server/routes.py` mounts `/v1` and answers `GET /v1/health` with `{status, version, uptime}` exactly as `ARCHITECTURE.md § 9.1` specifies; `server/schemas.py` holds `HealthResponse` (frozen, `extra="forbid"`) and is the source the P0-11 TS generator will read. `logging_setup.py` writes one JSON object per line to a **rotating** `%LOCALAPPDATA%\Aegis\logs\core.log` (5 MB × 5 — `REVIEW.md § 2` forbids an unbounded log) and to **stderr**, never stdout (Decision Log); `configure_logging()` is idempotent, and `extra={...}` fields merge into the payload without being able to overwrite the level, logger or timestamp. `__main__.py` parses `--port` and serves on `127.0.0.1` with `log_config=None` so uvicorn's own lines flow through the same formatter. Deps added: fastapi, uvicorn, pydantic (+ dev `httpx2`, which starlette's TestClient now requires; one targeted `filterwarnings` ignore for a deprecation raised inside starlette itself). 28 new tests (60 pytest, 67 desktop, 37 renderer, all green); ruff + `mypy --strict` clean. Exercised by hand: server up, `/v1/health` 200 with a growing uptime, `/health` and `/openapi.json` 404, log file and stderr both filling, **0 bytes on stdout**. **Not** in this task, by design: the § 3.1 handshake, bearer auth and the peer-PID check — all P0-06, which is why the port must be passed explicitly for now | P0-06 startup handshake |
