# REMEMBER.md

> **READ THIS FIRST, EVERY SESSION, BEFORE ANY CODE.**
> This is the project's memory. It is short on purpose. If you read nothing else, read this file.

---

## 1. What AEGIS is

A Windows desktop app that lets an AI agent **actually operate the user's PC** — see the screen, click, type, manage files, run commands, drive a browser — to complete tasks the user describes in plain language. The user watches every step live, takes over instantly by touching their own mouse or keyboard, and can hard-stop with a global hotkey. The user brings their own model API keys and chooses which model does what.

**One-line positioning:** *the computer-use agent you can actually trust with your machine.*

**What makes it different from a demo:** the safety layer. Anyone can wire a vision model to PyAutoGUI. Almost nobody ships scope enforcement, a hash-chained audit log, real undo, sub-100 ms human preemption, and a kill switch that works when the engine is hung. **That layer is the product.** When you are deciding what to build next and it isn't obvious, build the safety thing.

---

## 2. Document map

| File | What it is | When to read it |
|---|---|---|
| **REMEMBER.md** | This. Identity, invariants, decisions, glossary | Every session, first |
| **ARCHITECTURE.md** | Stack, processes, modules, APIs, security design | Before writing any code in a new area |
| **UI.md** | Every screen, component, state, copy rule | Before touching the renderer |
| **PROGRESS.md** | The task tracker. 97 numbered tasks across 9 phases | Every session, to pick work and to record it |
| **REVIEW.md** | Definition of done + the security gate | Before marking anything `DONE` |
| **RECOVERY.md** | Undo, crash handling, and dev-session recovery | When something is broken or half-finished |

`CLAUDE.md` at the repo root exists only to point here.

---

## 3. Invariants — never break these, never "temporarily" break these

1. **The human always wins.** Any real physical input pauses the agent within 100 ms. The agent never fights for the cursor, never re-takes focus while paused, never auto-resumes.
2. **The kill switch works when everything else doesn't.** It lives in the Electron main process, not the Python core, so a hung core cannot disable it.
3. **No held key survives a stop.** Every abort path releases Ctrl/Alt/Shift/Win. A stuck modifier key is a P0 bug, always.
4. **`DANGEROUS` never runs unattended.** There is no setting, no autonomy level, no "expert mode" that changes this. It is a product commitment.
5. **The FORBIDDEN list is compiled in and unreachable from the UI.** System directories, credential stores, browser cookie/login databases, `.ssh`/`.aws`/`.gnupg`, wallet files, Defender/firewall/UAC, the audit log itself, Aegis' own install directory.
6. **Approval timeouts deny.** Walking away never authorises anything.
7. **The agent never types the user's credentials.** It stops and asks the human to log in. There is no password-storage feature and there never will be.
8. **Screenshots are redacted before they leave the process.** Password fields are black-boxed in the image bytes, not just hidden in the UI.
9. **Keys live in Windows Credential Manager (DPAPI).** Never in argv, env vars passed to children, logs, error payloads, or the renderer.
10. **Nothing leaves the machine except calls to the model provider the user chose.** There is no Aegis backend, no telemetry by default, and an egress allowlist enforces it.
11. **Journal before you act.** The undo record is written before the mutating operation runs.
12. **Delete is never delete.** It is a move to the shadow store.
13. **One action per model turn, and every action is verified.** No batching. Unverified ≠ successful.
14. **The core never outlives the UI.** A headless process with mouse control and no window is the failure mode we refuse to ship.
15. **The UI is a pure function of the event stream.** No polling for task state.

---

## 4. Locked decisions (change only with a new Decision Log entry)

- Windows-only for v1. No macOS, no Linux, no exceptions until v1 ships to real users.
- Electron + React + TypeScript for the shell; Python 3.11 for the agent core; three processes, always.
- We own the agent loop. No LangChain, no CrewAI, no agent framework.
- UIAutomation tree first, vision second, raw coordinates last.
- Own `SendInput` ctypes wrapper, not PyAutoGUI.
- Three model **roles** (Planner / Grounder / Utility), each independently mapped by the user to any provider.
- Five of seven providers are OpenAI-compatible — one adapter with different base URLs, not seven clients.
- Bring-your-own-key. We never resell inference. Future pricing is a flat license on the app, never metered on tokens — so never couple a feature to token usage.
- PyInstaller **onedir** (not onefile) + electron-builder NSIS, per-user install, no admin.
- SQLite for everything local. No server, no cloud sync in v1.

---

## 5. Decision Log

Append here whenever you make a call that a future session could reasonably question. Format: date · decision · why · what we rejected.

| Date | Decision | Why | Rejected alternative |
|---|---|---|---|
| 2026-09-05 | Electron over Tauri | Solo dev velocity; Rust glue debugging cost outweighs the ~140 MB size saving | Tauri |
| 2026-09-05 | Python core as a separate process, not embedded | Windows automation + AI libraries are Python-native; process isolation is also a security boundary | Node-only with native addons |
| 2026-09-05 | Own the agent loop | Frameworks hide the exact place we need the Guardian to sit | LangChain / CrewAI |
| 2026-09-05 | Own SendInput wrapper | PyAutoGUI drops events, can't tag events for preemption, can't abort mid-sequence | PyAutoGUI |
| 2026-09-05 | UIA tree before pixels | Semantic elements are dramatically more accurate and far cheaper in tokens | Pure vision / coordinate clicking |
| 2026-09-05 | Approval timeout = deny | The failure mode of auto-allow is unbounded | Auto-allow after timeout |
| 2026-09-05 | No password storage, ever | Turns a productivity tool into a credential-theft target | Encrypted credential autofill |
| 2026-09-05 | Model roles instead of one model | Cost control + lets users run cheap/local models for the boring 80% | Single model per task |
| 2026-09-07 | Docs stay in `aegis-docs/`, not the repo root | They were already there and every cross-reference points at that path; `CLAUDE.md`/`AGENTS.md` at the root point into it. This is the only deviation from `ARCHITECTURE.md § 4` | Moving six files to the root |
| 2026-09-07 | Tailwind v4 (CSS-first `@theme`), no `tailwind.config.js` | v4 is the current line, shadcn/ui supports it, and design tokens live in one CSS file instead of two places | Tailwind v3 + JS config |
| 2026-09-07 | Electron MAIN builds with plain `tsc` to ESM, no bundler | The scaffold needs no bundling; adding electron-vite before there is a window to bundle is premature. Revisit at P0-02 if HMR on MAIN is wanted | electron-vite / esbuild now |
| 2026-09-07 | `requires-python = ">=3.11,<3.14"` | Target runtime stays 3.11 (packaged interpreter), but the dev machine has only 3.13; the range lets both work. Pin to 3.11 exactly when PyInstaller lands in P7 | Hard-pinning 3.11 and blocking local dev |
| 2026-09-07 | Preload is `src/preload/bridge.cts`, emitting `bridge.cjs` | A **sandboxed preload cannot be an ES module**, and MAIN is ESM. `.cts` makes `tsc` emit CommonJS for that one file with no bundler and no second config. Deviation from the `bridge.ts` in `ARCHITECTURE.md § 4`; renaming it back silently breaks the window | `sandbox: false` + `.mjs` preload (weaker), or adding a bundler |
| 2026-09-07 | `src/main/window.ts` and `src/main/tray.ts` added | `ARCHITECTURE.md § 4` lists only index/supervisor/hotkeys/updater/ipc. Window and tray lifecycle are cohesive units that would otherwise bloat `index.ts`; the pure parts (`renderer-entry.ts`, `tray-menu.ts`) are split out so they are testable without an Electron process | One long `index.ts` |
| 2026-09-07 | Vitest in `apps/desktop`; root `pnpm test` also runs `pytest` | `pnpm test` previously passed while running **zero** tests, so the `REMEMBER.md § 10` / `RECOVERY.md § 6.1` start-of-session gate could never fail. `apps/desktop/tsconfig.json` is now the default project (src + tests) and `tsconfig.build.json` is what emits, so tests are type-checked and linted but never packaged | Leaving TS untested until the renderer needs a runner |
| 2026-09-09 | Light palette derived and written into `UI.md § 2` | `UI.md § 2` gave only the dark values but demands both themes be defined outside media queries. Every light value was contrast-checked ≥ 4.5:1 on its own surface; the amber had to darken to `#A96500` because `#F5B547` fails on white, exactly as `§ 10` warns | Leaving light undefined, or auto-inverting the dark palette |
| 2026-09-09 | Tailwind's stock colour / font-size / radius scales are cleared (`--color-*: initial`) before the AEGIS ones are declared | An off-token colour or a 15px font is a `REVIEW.md § 3` failure, and no lint rule catches `text-neutral-500`. Cleared namespaces mean Tailwind generates no utility for them at all, so the mistake stops compiling | Relying on review to catch off-token classes |
| 2026-09-09 | `cn()` uses `extendTailwindMerge`, not bare `twMerge` | With the stock scales cleared, tailwind-merge cannot tell `text-md` (a size) from `text-danger` (a colour) and silently keeps both. It has to be told the same scales the CSS declares | Bare `twMerge` |
| 2026-09-09 | Theme is CSS-only for now: dark on `:root`, light via `prefers-color-scheme`, `data-theme` overriding both | Following the OS needs no JavaScript, and the attribute hook is already there for the `UI.md § 8.6` toggle to set. A theme store before there is a Settings screen would be state with one writer and no reader | A theme store/provider in P0-03 |

---

## 6. Glossary — use these words exactly, in code and in UI

| Term | Means |
|---|---|
| **Task** | One user goal, start to finish |
| **Step** | One perceive→plan→act→observe cycle within a task |
| **Tool** | A capability the agent can invoke (`fs.move`, `input.click`) |
| **Guardian** | The policy engine every tool call passes through |
| **Risk tier** | `SAFE` / `CAUTION` / `DANGEROUS` / `FORBIDDEN` |
| **Autonomy** | The user's setting: Observe / Guided / Standard / Trusted |
| **Scope** | The folders + apps the agent may touch for this task |
| **Preemption** | The agent yielding because the human touched the mouse/keyboard |
| **Kill switch** | The global hotkey hard stop |
| **Grounding** | Turning "click the Date column" into a verified screen point |
| **Observation** | A screenshot + UIA tree captured at one moment |
| **Journal** | The write-ahead undo record |
| **Shadow store** | Aegis' own trash, where "deleted" files actually go |
| **Set-of-mark** | Numbered overlays on candidate elements so the model picks an id, not a coordinate |
| **Core** | The Python sidecar process (`aegis-core.exe`) |
| **MAIN** | The Electron main process |

---

## 7. Facts about how this project is built

- **Solo developer** (Dhaval, Windows, VS Code, GitHub Desktop, comfortable in Python/React), building this with Claude Code across many sessions. Optimise for: small commits, clear task boundaries, and documents that carry context between sessions. Avoid: heroic multi-day refactors, clever abstractions with one caller.
- **Sessions are the unit of work.** Assume the next session remembers nothing. `PROGRESS.md § 12` is the handoff.
- **Commit messages start with the task ID** (`P3-05: …`) so history maps to the tracker.
- **Start `P7-09` (antivirus false-positive submissions) in week one.** It takes weeks of calendar time and blocks launch. Everything else is code; that one is waiting.
- **The benchmark suite (`P4-12`) is the definition of "is it good".** Build it early, run it often, record cost baselines.

---

## 8. Things that will go wrong (forewarned)

1. **Grounding accuracy is 60% of perceived quality.** Budget real time in P2/P3; do not rush to P5 because tool code is easier to write.
2. **Prompt injection through screen content** is the newest and least-defended attack class in computer-use agents. `P8-01`/`P8-02` are not optional polish.
3. **Antivirus will flag you.** Signing + vendor submissions, started early.
4. **Vision-model cost adds up fast** — one image per step, downscaled, with a budget guard.
5. **DPI and multi-monitor bugs** are the single most common source of "it clicked the wrong place". `P2-01` deserves more care than its one line suggests.
6. **Scope creep to macOS** will feel tempting the moment Windows v1 works. Don't.

---

## 9. Incidents

Every incident gets a row, a root cause, and the test that now prevents it. Empty is good.

| Date | What happened | Root cause | Test added |
|---|---|---|---|
| — | — | — | — |

---

## 10. Session checklist (copy this into your working notes)

**Start**
- [ ] Read this file.
- [ ] Read `PROGRESS.md § 1` and the last line of `§ 12`.
- [ ] `pnpm install && pnpm build && pnpm test` — green before touching anything.
- [ ] Pick one task. Set it `WIP`.

**End**
- [ ] Tests green; built app exercised by hand once.
- [ ] `REVIEW.md § 1` walked; `§ 5` too if the task is security-gated.
- [ ] `PROGRESS.md` status updated + session log line appended.
- [ ] Decision Log updated if you decided anything.
- [ ] Committed with the task ID in the message.
