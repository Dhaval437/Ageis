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
| **Current task** | P0-12 — fix the root `clean` script (next) |
| **Last session** | 2026-09-14 — P0-11 Pydantic→TS type generation |
| **Overall** | 13 / 105 tasks |
| **Ship target for v1** | Windows installer, Standard autonomy, fs + input + shell + browser tools |

### Phase progress

| Phase | Name | Tasks | Done | Status | Exit gate |
|---|---|---|---|---|---|
| P0 | Foundations & plumbing | 18 | 11 | 🟨 | App launches, core handshake works, one round-trip |
| P1 | Model layer | 10 | 0 | ⬜ | Chat with any of 3 providers; keys stored in DPAPI |
| P2 | Perception | 9 | 0 | ⬜ | Agent can describe the screen and list clickable elements |
| P3 | Actuation + safety spine | 15 | 2 | 🟨 | Agent clicks correctly; kill switch and preemption both < targets |
| P4 | The agent loop | 12 | 0 | ⬜ | 8 of 10 benchmark tasks complete unattended |
| P5 | Tool families | 13 | 0 | ⬜ | fs / shell / browser all behind Guardian |
| P6 | Recovery, undo, audit | 10 | 0 | ⬜ | Every destructive op is undoable; log chain verifies |
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
| P0-06 | **Startup handshake**: ephemeral port, token over stdin pipe, stdout JSON line, peer-PID check | P0-02, P0-05 | DONE | 2026-09-09. Steps 1–3 and 5 of `ARCHITECTURE.md § 3.1`. MAIN: `main/handshake.ts` (`startCore` — 256-bit token over the stdin pipe, one JSON line read back, child killed on every failure path) and `main/core-gateway.ts` (the only HTTP client; adds origin, port, `/v1` and the bearer token, never an `Origin`). Core: `server/handshake.py` (`read_token`/`bind_loopback`/`announce`) and `server/auth.py` (`SessionAuthMiddleware` — no `Origin`, constant-time token, peer-PID check via psutil). Steps 4 and 6 — health check, 3-strike respawn, kill-on-exit — are P0-07, so this is **not wired into `index.ts` yet**: a spawn without a lifecycle is invariant 14's failure mode |
| P0-07 | Supervisor: spawn, health-check, 3-strike respawn, kill core when MAIN exits | P0-06 | DONE | 2026-09-10. `main/supervisor.ts` (`createSupervisor` — health check on `/health`, restart on an unexpected exit, 3 attempts inside a 60 s window then `unavailable`; `resolveCoreLaunch` picks the packaged `aegis-core.exe` or the dev venv interpreter, `AEGIS_CORE_COMMAND` overrides both) and the wiring in `index.ts` (`rest.core` is the live gateway or `null`; `before-quit` stops the core). Core: `aegis_core/parent_watch.py` watches the supervising PID and stops uvicorn when it dies, so a MAIN killed from Task Manager still takes the core with it. Measured: real app, MAIN force-killed → core gone in **0.20 s**; graceful quit → **0.33 s**; parent watch in isolation (supervisor killed, pipes untouched) → **0.71 s**. Budget is 2 s. **Not** here: the 1/s watchdog (P3-07) and the Engine-unavailable screen (P0-17) |
| P0-08 | WebSocket event hub + `seq` replay from memory (SQLite replay lands in P6) | P0-05 | DONE | 2026-09-13. `server/hub.py` (`EventHub` — thread-safe `publish`, 2048-event replay buffer, per-subscriber queue of 512 then disconnect with `4429`), `WS /v1/stream` in `server/routes.py`, `StreamEvent` + `EventType` in `server/schemas.py`, hub on `app.state.hub`. `since` is exact or refused (`4410`), never replayed with a hole. Close codes in `ARCHITECTURE.md § 9.2`. **Nothing publishes yet**, and MAIN does not connect yet (P0-09) |
| P0-09 | Renderer event-stream client + Zustand store; UI is a pure function of the stream | P0-08, P0-03 | DONE | 2026-09-14. MAIN: `main/core-stream.ts` holds the one WebSocket (`ws`, token in MAIN, no `Origin`, 4 MiB frame cap). It reconnects to the same core with `?since=` and backoff, sends `reset` for a new core, for `4410`/`4400`/`1003` and on every page load, and reports `connecting`/`live`/`down`/`unavailable`. `supervisor.onSession` tells it which core is live. Contract: `CoreStreamMessage` in `shared/bridge.ts`, on the existing `aegis:core:event` channel (no new bridge member). Renderer: `lib/stream-event.ts` validates every message, `stores/stream.ts` (`reduceStream`, bounded to 1000 events), `lib/stream-client.ts` subscribes before first render, and the titlebar shows the engine note. `StreamEvent`/`EVENT_TYPES` are **hand-seeded** in `shared/api.ts` for P0-11 to replace. `SECURITY GATE: reviewed against REVIEW.md §5 — claude-code, 2026-09-14` |
| P0-10 | SQLite bootstrap + migration runner + the schema from `ARCHITECTURE.md § 7` | P0-05 | DONE | 2026-09-14. `storage/db.py`: `connect()` (WAL verified, `synchronous=FULL`, foreign keys on, `trusted_schema=OFF`, 5 s busy timeout, SQLite ≥ 3.38 required), `migrate()` (`PRAGMA user_version`; each migration and its version bump in one transaction; a DB newer than the build is refused; a misnumbered list is refused), `bootstrap()` run by `__main__.py` after the token and **before** the handshake line, exit code 4 on failure. `storage/migrations/m0001_initial.py`: all eight § 7 tables, `STRICT`, locked vocabularies `CHECK`ed, JSON columns `json_valid`, `audit` append-only by trigger. No connection is held yet — the first reader/writer decides how connections are shared. A corrupt DB currently makes the core fail to start (→ supervisor `unavailable`); recovering from that is P6-10 |
| P0-11 | Pydantic→TS type generation script wired into `pnpm dev` | P0-05, P0-03 | DONE | 2026-09-14. `core/aegis_core/server/typegen.py` introspects `server/schemas.py`: each `BaseModel` defined there → `readonly` interface with JSDoc, each module-level `Literal` alias → `const NAME_S` tuple + derived type, serialised shape, unknown annotations raise. `scripts/gen-types.ts` runs it with the venv interpreter (60 s timeout), prettier-formats, writes `packages/shared/src/api.ts`. `pnpm dev` regenerates first; `pnpm test` starts with `pnpm gen:types:check` (exit 1 if stale — there is no CI yet, see P0-18). The hand-seeded `EVENT_TYPES`/`EventType`/`StreamEvent` and the hand-written `RiskTier` are now generated with the same names and shapes; `HealthResponse` is new in TS. `RiskTier` moved into `schemas.py`; the unused `SHARED_PACKAGE_NAME` placeholder is gone |
| P0-12 | Fix the root `clean` script: `rimraf node_modules` deletes the rimraf it is running from, so it always exits 1 half-done | — | TODO | Found during P0-02. Run it and you must `pnpm install` again |
| P0-13 | Storybook for the renderer, with the four required stories per component | P0-03 | TODO | Found during P0-03. `REVIEW.md § 3` demands default/loading/empty/error stories, and the `UI.md § 12` inventory starts landing at P3-12 — this must exist before it does |
| P0-14 | Bundle Inter + JetBrains Mono as local `woff2` and self-host them | P0-03 | TODO | Found during P0-03. `UI.md § 2` names both; the app must render correctly offline, so no webfont CDN — the CSP has no `font-src` beyond `'self'` for that reason |
| P0-15 | Maximise/restore: add `window.maximize` to the bridge surface, then wire the `□` button | P0-04 | TODO | Found during P0-04. The `UI.md § 4.1` sketch shows `– □ ×` but `ARCHITECTURE.md § 9.3` has only `minimize`/`close`, so the button was left out rather than the surface widened silently. Needs a `REVIEW.md § 5` sign-off and an `ARCHITECTURE.md § 9.3` edit |
| P0-17 | **Engine unavailable** screen (`RECOVERY.md § 4`): Restart engine / Open logs / Copy report, driven by the supervisor's `unavailable` state | P0-07, P0-09 | TODO | Found during P0-07. The supervisor reaches `unavailable` after 3 failed attempts and today nothing but the log says so |
| P0-16 | Narrow the peer-PID check so a job the **core** spawns cannot call the core's own API | P0-06, P5-08 | TODO | Found during P0-06. `ARCHITECTURE.md § 3.1` step 5 says "the supervising PID's child chain", and that chain includes the PowerShell and Playwright jobs the core launches — so a compromised job could drive the agent through its own REST API. Harmless today (no jobs exist); must be closed before P5-08 ships. Security-gated |
| P0-18 | CI workflow (GitHub Actions, `windows-latest`): `pnpm install --frozen-lockfile`, core venv + `pip install -e .[dev]`, then `pnpm build`, `pnpm lint`, `pnpm typecheck`, `pnpm py:lint`, `pnpm py:typecheck`, `pnpm test` | P0-11 | TODO | Found during P0-11. The task line said "CI fails if generated types are stale" and the repo has no CI at all. The check is in `pnpm test`, so the workflow only has to run it. The `py:*` scripts hard-code `.venv\Scripts`, so the job must create the venv at `core/.venv` |

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
| P3-15 | MAIN's own modifier-release path, run **first** when the core dies mid-task | P3-06, P0-07 | TODO | Found during P0-07. `RECOVERY.md § 4`'s absolute rule: a dead agent must not leave Ctrl/Alt/Shift/Win held, and the core cannot release them once it is gone — so the release has to live in MAIN, on the supervisor's death path |
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
| P6-10 | `aegis.db` protection per `RECOVERY.md § 5`: backup copy on every start (keep 5), `PRAGMA quick_check` on open, corrupt DB → rename to `aegis.db.corrupt-<ts>` and start fresh, tell the user where it went | P0-10, P0-17 | TODO | Found during P0-10. Today `bootstrap()` raises `StorageError` on an unreadable DB, the core exits 4, and after 3 attempts the supervisor gives up — so one corrupt file bricks the app until this lands |

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
| 2026-09-09 | claude-code | P0-06, P0-16 | Startup handshake, both halves. **MAIN:** `main/handshake.ts` mints a 256-bit token, spawns the core, writes the token to the **stdin pipe** and closes it, then reads exactly one JSON line off stdout — with a chunk-boundary-safe reader, an 8 KiB cap, a 15 s timeout, and `child.kill()` on *every* failure path so a bad greeting never leaves a process with mouse control running. `main/core-gateway.ts` is now the only thing that speaks HTTP to the core: it adds the origin, port, `/v1` prefix and bearer token, sends no `Origin`, times out every call, refuses redirects, caps the response, and reports transport errors without echoing anything that could carry the token. **Core:** `server/handshake.py` (`read_token` — a missing token is fatal, since a bound port with no credential must never exist; `bind_loopback` — the socket is bound *before* uvicorn so the ephemeral port can be announced; `announce` — one line, then stdout is closed and `sys.stdout` repointed at a sink so a stray `print` can neither corrupt the channel nor crash the core). `server/auth.py` is a pure-ASGI `SessionAuthMiddleware`: `Origin` present → reject, then a constant-time bearer compare, then the psutil peer-PID check, all answering one identical bare `401` so a prober cannot learn which check it tripped. `create_app()` now *requires* a `SessionAuth` and the middleware wraps FastAPI's own 404s. **The bug the harness caught:** `os.getppid()` in the core does not name MAIN — the venv `python.exe` re-execs, so MAIN is a *grandparent* — and the peer check refused MAIN itself. MAIN now passes `--supervisor-pid` explicitly (Decision Log). 64 new tests (112 desktop, 37 renderer, 120 pytest, all green); ruff, `mypy --strict`, eslint, tsc clean. Exercised by hand against the real core with a throwaway harness: handshake in ~900 ms, authorised `GET /v1/health` → 200; no token, wrong token, right-token-plus-`Origin`, and an unknown route unauthenticated → all `401` with byte-identical bodies; an unknown route *with* the token → 404; **a separate process holding the correct token → 401** (`untrusted_peer`); **0 bytes** on stdout after the handshake line; no orphan core after the harness exits. Built Electron app launched by hand — unchanged, 0 orphans. Logged P0-16: `§ 3.1`'s "child chain" also admits the jobs the *core* spawns, which must be narrowed before P5-08 | P0-07 supervisor: health check, 3-strike respawn, kill-on-exit, and wiring `rest.core` into `index.ts` |
| 2026-09-10 | claude-code | P0-07, P0-17, P3-15 | Supervisor, both halves. **MAIN:** `main/supervisor.ts` is the lifecycle around `handshake.ts` — it health-checks `GET /v1/health` the moment the core announces a port (two tries; anything but a 200 kills that core), restarts a core that dies on its own, allows **3 attempts inside a 60 s window** and then settles on `unavailable` instead of spawning forever, and kills the core on `stop()` with a SIGTERM→SIGKILL escalation. `gateway()` is the live core or `null`, which is what `rest.core` now returns in `index.ts`, so the renderer keeps seeing an honest `unavailable` while the core is down. `resolveCoreLaunch` resolves the packaged `resources/core/aegis-core.exe` or the dev `core/.venv/Scripts/python.exe -m aegis_core`, with `AEGIS_CORE_COMMAND` overriding both. **Core:** `aegis_core/parent_watch.py` polls the supervising PID every 0.5 s on a daemon thread, identifies it by **PID + creation time** so a reused PID cannot pass for MAIN, treats `AccessDenied` as alive (only a death stops the core), and on loss asks uvicorn to stop with a 1 s hard-exit backstop — 2 s budget, met with room. 26 new tests (142 desktop, 37 renderer, 132 pytest, all green); ruff, `mypy --strict`, eslint, tsc, prettier clean. Exercised by hand on the **real built app**: core spawned with `--supervisor-pid`, `/v1/health` 200, MAIN force-killed → core gone in **0.20 s** with 0 orphans, graceful quit → **0.33 s**; and the parent watch on its own, with a supervisor whose death does not touch the core's pipes → **0.71 s** and a `core.supervisor_lost` log line. (`RECOVERY.md § 6.3.1` again: `ELECTRON_RUN_AS_NODE=1` in the agent terminal makes Electron refuse to launch — clear it first.) Logged P0-17 (the Engine-unavailable screen has no renderer yet) and P3-15 (`RECOVERY.md § 4` wants MAIN to release held modifiers when the core dies, and MAIN has no input path until P3-06) | P0-08 WebSocket event hub |
| 2026-09-13 | claude-code | P0-08 | WebSocket event hub, core side. `server/hub.py`: `EventHub.publish()` is callable from any thread (the agent loop and preemption watcher are not on the event loop), stamps `seq` under an `RLock` and fans out with `call_soon_threadsafe`, so it never waits on a subscriber. Replay comes from a 2048-event in-memory buffer. Subscribing snapshots the backlog and registers under the same lock, so nothing is missed or duplicated at the boundary. A `since` that is evicted, or ahead of this core, is refused with `4410` rather than replayed with a hole. A subscriber more than 512 live events behind is cut off with `4429` and catches up with `since`. `WS /v1/stream` in `server/routes.py` accepts before refusing so the client sees a close code; malformed `since` → `4400`, a client message → `1003` (the stream is one-way). Reader and writer run in an **anyio task group**: the first draft used bare `asyncio` tasks and failed intermittently when the handler was cancelled, and a real server shutdown takes the same path. Deps: `websockets` (uvicorn cannot upgrade without it) and `anyio` made explicit. 34 new tests (166 pytest, 127 desktop, 37 renderer, all green; the core suite run 3× with no flakes); ruff, `mypy --strict` clean. One of them runs real uvicorn on a loopback socket with a cross-thread publisher, because TestClient would not notice a missing WS library. Test-only gotcha: starlette's `websocket_connect` **writes into the headers dict you pass it**, so a shared `AUTHORIZED` constant broke later tests. Exercised by hand against the real core via the § 3.1 handshake: authed connect opens; `since=5` on a fresh core → `4410`; `since=abc` → `4400`; no token and `Origin` → HTTP 403; client message → `1003`; core gone after the harness ends. Contract written into `ARCHITECTURE.md § 9.2`. Not done: nothing publishes events yet, and MAIN has no WS client (moved into the P0-09 note). Desktop shows 127 tests where the P0-07 line says 142. No TS was touched this session; worth a look | P0-09 renderer event-stream client (incl. MAIN's WS client) |
| 2026-09-14 | claude-code | P0-09 | Event stream end to end: core → MAIN → renderer store → titlebar. **MAIN:** `main/core-stream.ts` owns the WebSocket, because the token must stay in MAIN and Electron 32's Node has no usable `WebSocket`, hence `ws`. It tracks the last `seq` forwarded and reconnects to the same core with `?since=` (250 ms → 5 s backoff), dropping duplicates across the boundary. When the supervisor reports a *different* core (`supervisor.onSession`, new), on close `4410`/`4400`/`1003`, and on every renderer `did-finish-load`, it drops the cursor, sends `reset` and replays from scratch. It announces `connecting`/`live`/`down`/`unavailable`, taking `down` vs `unavailable` from supervisor state. An unreadable frame is dropped and only its size is logged. **Contract:** `CoreStreamMessage` (`event`/`reset`/`connection`) over the existing `aegis:core:event` channel. No bridge member was added; the § 5 walk is in the P0-09 row. **Renderer:** `lib/stream-event.ts` validates every message (the payloads carry scraped screen text). `stores/stream.ts` is a pure `reduceStream` behind Zustand: connection, `hasBeenLive`, `lastSeq`, the last 1000 events, a rejected count; out-of-order or duplicate `seq` is ignored, and `reset` keeps connection state. `connectStream()` runs in `main.tsx` before the first render. The titlebar `EngineStatus` shows *Starting the engine…* / *Reconnecting…* / a red *The engine is not running.* (`UI.md § 4.1` updated). `StreamEvent` types are hand-seeded in `shared/api.ts` for P0-11 to replace (Decision Log). Deps: `ws`, `@types/ws`, `zustand`. 18 desktop tests (incl. a real `ws` server checking the token is sent and no `Origin` is) and 31 renderer tests: 145 desktop, 68 renderer, 166 pytest, all green; tsc, eslint (0 warnings), prettier clean. **Exercised on the real built app** over CDP: *Starting the engine…* → idle 1.1 s after first paint; core force-killed → *Reconnecting…* immediately → respawned core (new PIDs) → idle 1.2 s later; page reload → reset → idle in 0.3 s; quit → 0 orphan cores. Not shown by hand: events actually flowing in Electron, because nothing in the core publishes yet (covered by the real-socket tests on both sides). Gotcha for the next session: Python `write_text` on Windows writes CRLF, which prettier flags; write bytes or run prettier after | P0-10 SQLite bootstrap |
| 2026-09-14 | claude-code | P0-10, P6-10 | SQLite bootstrap. `core/aegis_core/storage/db.py`: `connect()` creates the folder and opens `%LOCALAPPDATA%\Aegis\aegis.db` in autocommit mode, **verifies** WAL rather than assuming it, sets `synchronous=FULL` (a journal row a power cut can revoke is not write-ahead), foreign keys on, `trusted_schema=OFF`, a 5 s busy timeout, and refuses SQLite < 3.38 (`STRICT` + `json_valid`). `migrate()` versions by `PRAGMA user_version` and runs `BEGIN IMMEDIATE; <sql>; PRAGMA user_version=N; COMMIT` as one script, because `executescript` silently commits a transaction opened before it. It rolls back and raises on failure, tolerates another process having applied the same step, and refuses a DB newer than the build and a gapped or duplicated migration list. Migrations are **Python modules listed by hand** in `migrations/__init__.py`, not discovered `.sql` files, so PyInstaller cannot drop one. `m0001_initial.py`: the eight § 7 tables as `STRICT`, with `CHECK`s on task status, risk tier, Guardian decision, approval choice, 0/1 booleans, JSON validity and 64-char hashes; `choice`/`decided_at` set together; unique `(task_id, idx)` and `(scope, key)`; cascades from `tasks`; `audit` UPDATE/DELETE blocked by trigger. `journal` gained `forward_json` + `created_at` from `RECOVERY.md § 2.1`, and `cost_cents` is `REAL` (Decision Log; § 7 updated). `__main__.py` calls `bootstrap()` after reading the token and before binding, so a DB the core cannot use is a failed start (exit 4, `core.storage_failed`) rather than a failed task. 31 new tests (197 pytest; desktop/renderer untouched); ruff, `mypy --strict` clean. Exercised on the **real built app**: fresh profile → `aegis.db` created, `journal_mode=wal`, `user_version=1`, all eight tables, `storage.migrated` + `storage.ready` logged; force-killed and relaunched on the same DB → `storage.ready` only (no re-migration), core up; 0 orphan cores both times. Gotcha: `ruff format aegis_core` reformats `actuation/input.py` on its own — scope the formatter to the files you touched. Logged P6-10 (a corrupt DB currently bricks startup) | P0-11 Pydantic→TS type generation |
| 2026-09-14 | claude-code | P0-11, P0-18 | Pydantic→TS type generation. **Core:** `server/typegen.py` renders `schemas.py` by introspection rather than JSON Schema, so `EventType` keeps its name (Decision Log). `BaseModel`s defined in the module → `readonly` interfaces; `Literal` aliases → `const NAME_S = [...] as const` + `(typeof NAME_S)[number]`; docstrings, field descriptions and string literals after an alias → JSDoc (`*/` escaped); `str`/`int`/`float`/`bool`/`None`/`Literal`/unions/`list`/`dict[str, T]`/`JsonValue`/sibling models are supported, and anything else, an imported model or a non-identifier alias raises `TypeGenError` naming the field. `main()` writes UTF-8 + LF bytes to stdout (Windows text-mode `print` would write CRLF). `RiskTier` moved into `schemas.py`, and the `#:` comment on `EventType` became an attribute docstring. **Script:** `scripts/gen-types.ts` runs `python -m aegis_core.server.typegen` from `core/.venv` with a 60 s timeout, formats it with the repo prettier config and writes `api.ts`, or with `--check` exits 1 if it differs. It runs on Node's built-in type stripping (`engines` → `>=22.18.0`, root `"type": "module"`, `scripts/tsconfig.json` with `erasableSyntaxOnly`, `@types/node` at root, `pnpm typecheck` now includes it). Wiring: `pnpm gen:types`, `pnpm gen:types:check`; `pnpm dev` generates first; `pnpm test` checks first. `api.ts` is now fully generated: same names and shapes for `EVENT_TYPES`/`EventType`/`StreamEvent`/`RiskTier` (plus a `RISK_TIERS` tuple), and `HealthResponse` is new. 16 new pytest tests (213 pytest, 145 desktop, 68 renderer, all green); ruff, `mypy --strict`, tsc (incl. scripts), eslint, prettier clean. Exercised: the committed file passes `--check`; adding a field to `HealthResponse` makes `pnpm test` fail with the stale message and reverting makes it pass. The **built app** launched: core spawned, `/v1/health` 200, the renderer's stream connected (`stream.connected` logged) on the generated `EVENT_TYPES`; Electron force-killed → 0 orphan cores. Logged P0-18: the task line says "CI fails" and there is no CI workflow in the repo. Gotcha: the Edit tool treats a JSON `\u2014` escape and a literal em dash as the same string, so restoring one needs a byte-level write | P0-12 root `clean` script |
