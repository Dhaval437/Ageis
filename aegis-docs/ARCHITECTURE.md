# ARCHITECTURE.md

> **Project:** AEGIS — an AI agent that operates the user's Windows PC on their behalf.
> **Codename:** `aegis` (rename freely — it appears only in `PRODUCT_NAME` in `packages/shared/src/brand.ts` and `pyproject.toml`).
> **Status:** design locked for v1. Any deviation must be recorded in `REMEMBER.md § Decision Log`.
> **Read order for a new session:** `REMEMBER.md` → this file → `PROGRESS.md`.

---

## 1. What we are building (one paragraph)

A commercial, installable Windows desktop application. The user types (or speaks) a task in plain language. An AI agent then *actually operates the computer* — it looks at the screen, reads the accessibility tree of windows, moves the mouse, types, opens apps, edits files, runs shell commands and drives a browser — until the task is done. The user watches every step live, can interrupt at any moment by simply touching their own mouse or keyboard (the agent yields instantly), and can hard-stop everything with a global kill-switch hotkey. The user chooses which AI model powers the agent (cloud or local) and supplies their own API keys. Security is the headline feature, not an afterthought: nothing dangerous happens without an explicit, informed approval, and everything that happens is written to a tamper-evident log.

**Non-goals for v1:** macOS/Linux, mobile, multi-user/team features, cloud sync, our own hosted inference, billing/subscriptions (scaffolding only — see §12).

---

## 2. Locked technology decisions

| Layer | Choice | Why |
|---|---|---|
| Shell / windowing | **Electron 32+** | Mature Windows packaging, overlay/always-on-top windows, global shortcuts, tray, auto-update. |
| UI | **React 18 + TypeScript + Vite** | Fast HMR, huge component ecosystem. |
| Styling | **Tailwind CSS + shadcn/ui + Radix primitives** | Accessible primitives for free; consistent dark UI. |
| UI state | **Zustand** (client state) + **TanStack Query** (server state) | Minimal boilerplate; avoids Redux ceremony. |
| Agent core | **Python 3.11**, packaged with **PyInstaller** (onedir) | Best-in-class Windows automation + AI libraries. |
| Core web layer | **FastAPI + Uvicorn**, bound to `127.0.0.1` on an **ephemeral port** | REST for commands, WebSocket for the event stream. |
| Local DB | **SQLite** via `sqlite3` (WAL mode) | Zero-config, transactional, file-portable. |
| Secrets | **Windows DPAPI** via `keyring` (Windows Credential Manager backend) | OS-level encryption at rest, tied to the user account. |
| Screen capture | **`mss`** (DXGI-backed), per-monitor DPI-aware | Fast, multi-monitor correct. |
| UI tree | **`uiautomation`** (UIA3) with **`pywinauto`** as fallback | Semantic elements beat pixels; huge accuracy win. |
| Input synthesis | **`SendInput` via `ctypes`** (own thin wrapper, not PyAutoGUI) | PyAutoGUI drops events and can't do scan codes; we need reliability + instant abort. |
| Input monitoring | **Low-level hooks** (`WH_MOUSE_LL`, `WH_KEYBOARD_LL`) via `ctypes` | Required for the "user always wins" preemption rule (§8.3). |
| Browser control | **Playwright (Chromium)** with a dedicated, isolated profile | Deterministic web automation instead of pixel-clicking a browser. |
| Packaging | **electron-builder → NSIS**, per-user install, no admin required | Standard `Setup.exe` experience; sidecar bundled as extraResources. |
| Auto-update | **electron-updater** against a static feed (GitHub Releases in dev, S3/R2 later) | Delta updates, signature-verified. |
| Testing | `pytest` + `pytest-asyncio` (core), `vitest` + React Testing Library (UI), `Playwright` (E2E) | — |
| Lint/format | `ruff` + `mypy --strict` (Python), `eslint` + `prettier` (TS) | — |

**Explicitly rejected:** Tauri (Rust glue slows a solo dev), pure-Python Qt UI (worse visual polish), LangChain/CrewAI as the agent framework (we own the loop — see §6.1), PyAutoGUI as the input layer.

---

## 3. Process model

Three OS processes, always. Never collapse them.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Aegis.exe  (Electron MAIN — Node.js)                                │
│  • window & tray lifecycle      • global kill-switch hotkey          │
│  • spawns/supervises the core   • auto-update                        │
│  • holds the session token      • the ONLY process the user sees     │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │ contextBridge (IPC)          │ HTTP + WS over 127.0.0.1
                │ no nodeIntegration           │ Bearer <session-token>
                ▼                              ▼
┌───────────────────────────────┐  ┌───────────────────────────────────┐
│ Renderer (Chromium, sandboxed)│  │ aegis-core.exe (Python sidecar)   │
│ • React UI only               │  │ • agent loop  • tool registry     │
│ • ZERO Node/fs/net access     │  │ • Guardian    • model router      │
│ • talks only to MAIN          │  │ • perception  • actuation         │
└───────────────────────────────┘  │ • SQLite      • audit log         │
                                   └───────────┬───────────────────────┘
                                               │ spawns on demand
                                   ┌───────────▼───────────────────────┐
                                   │ Sandboxed job processes           │
                                   │ (PowerShell runner, Playwright)   │
                                   │ low-integrity, no inherited env   │
                                   └───────────────────────────────────┘
```

### 3.1 Startup handshake (do not shortcut this)

1. MAIN generates a 256-bit random `SESSION_TOKEN` and picks port `0`.
2. MAIN spawns `aegis-core.exe --port 0 --token-fd <pipe>`; the token goes over a **pipe on stdin**, never as a command-line argument (argv is world-readable via WMI).
3. Core binds `127.0.0.1:<ephemeral>`, writes `{"port":N,"pid":P,"version":"..."}` as one JSON line on stdout, then closes stdout.
4. MAIN reads that line, stores the port, and health-checks `GET /v1/health` with the bearer token. Two failures in a row → kill and respawn (max 3 attempts, then show the Recovery screen from `RECOVERY.md § 4`).
5. Core rejects any request whose `Origin` header is present, or whose token does not match, with `401` — and **rejects any connection whose peer is not the supervising process itself**, identified by PID *and* creation time and checked via `psutil` on connect. Not the child chain: the core is a descendant of MAIN, so every job the core spawns (PowerShell, Playwright) would be inside that chain and could drive the agent through its own API. MAIN is the only client, and it opens every socket in its own process. (Narrowed by P0-16; this line used to say "the supervising PID's child chain".)
6. If MAIN dies, core detects the broken parent handle and exits within 2 s. **The core must never outlive the UI.** A headless agent with mouse control and no visible window is exactly the failure mode we refuse to ship.

---

## 4. Repository layout (monorepo, pnpm workspaces)

```
aegis/
├─ REMEMBER.md ARCHITECTURE.md UI.md PROGRESS.md REVIEW.md RECOVERY.md
├─ CLAUDE.md                      # points Claude Code at REMEMBER.md first
├─ package.json  pnpm-workspace.yaml  turbo.json
├─ apps/
│  ├─ desktop/                    # Electron MAIN + preload
│  │  ├─ src/main/                # index.ts, supervisor.ts, hotkeys.ts, updater.ts, ipc.ts
│  │  ├─ src/preload/             # bridge.ts  (the ONLY surface exposed to the renderer)
│  │  └─ electron-builder.yml
│  └─ renderer/                   # React app
│     ├─ src/screens/  src/components/  src/stores/  src/lib/
│     └─ vite.config.ts
├─ core/                          # Python agent
│  ├─ aegis_core/
│  │  ├─ server/                  # FastAPI app, routes, ws hub, auth
│  │  ├─ agent/                   # loop.py, planner.py, memory.py, context.py
│  │  ├─ models/                  # router.py, providers/*.py, schemas.py
│  │  ├─ tools/                   # registry.py + one module per tool family
│  │  ├─ perception/              # screen.py, uia_tree.py, ocr.py, grounding.py
│  │  ├─ actuation/               # input.py (SendInput), window.py, preempt.py
│  │  ├─ guardian/                # policy.py, rules.yaml, risk.py, approvals.py
│  │  ├─ recovery/                # journal.py, undo.py, snapshot.py
│  │  ├─ storage/                 # db.py, migrations/, audit.py, vault.py
│  │  └─ telemetry/               # local metrics only; OFF by default
│  ├─ tests/
│  └─ pyproject.toml
├─ packages/
│  ├─ shared/                     # TS types generated from Python Pydantic models
│  └─ ui/                         # shared React components
└─ scripts/                       # build-core.ps1, sign.ps1, release.ps1
```

**Type-safety rule:** Pydantic models in `core/aegis_core/server/schemas.py` are the single source of truth. `core/aegis_core/server/typegen.py` introspects that module and renders every `BaseModel` defined in it as a `readonly` interface, and every module-level `Literal` alias as a `const` tuple plus a derived type (`EventType` → `EVENT_TYPES`). `scripts/gen-types.ts` runs it, formats the result with prettier and writes `packages/shared/src/api.ts`. `pnpm dev` regenerates first; `pnpm test` runs `pnpm gen:types:check` and fails if the committed file is stale. Fields are emitted in their serialised shape (a defaulted field is not optional), and an annotation the generator does not know is an error, not `unknown`. Never hand-write a TS type that mirrors a Python model.

---

## 5. Model layer — bring-your-own-key, any provider

### 5.1 Provider abstraction

One interface, many adapters. Everything the agent needs from a model is expressed here:

```python
class ModelProvider(Protocol):
    id: str                       # "openai" | "anthropic" | "google" | "nvidia" | "ollama" | "openrouter" | "custom"
    async def chat(self, req: ChatRequest) -> AsyncIterator[ChatDelta]: ...
    def capabilities(self) -> Capabilities   # vision?, tool_calling?, json_mode?, ctx_window, cost_per_mtok
    async def validate_key(self) -> KeyStatus
```

Ship these adapters in v1:

| Adapter | Endpoint style | Notes |
|---|---|---|
| `openai` | OpenAI Chat Completions | Also serves **any OpenAI-compatible** base URL. |
| `anthropic` | Messages API | Native tool use + vision. |
| `google` | Gemini API | Vision, long context. |
| `nvidia` | `integrate.api.nvidia.com/v1` | **OpenAI-compatible** → subclass of `openai` with a different base URL and model list. |
| `openrouter` | OpenAI-compatible | One key, hundreds of models — great default for users who don't want many accounts. |
| `ollama` | `localhost:11434` | **Fully local, zero cost, zero data egress.** The privacy story. |
| `custom` | user-entered base URL + key | Any other OpenAI-compatible gateway (vLLM, LM Studio, Groq, Together). |

Because five of the seven are OpenAI-compatible, the real work is two adapters plus a registry of base URLs. **Do not write seven bespoke clients.**

### 5.2 Model roles (this is the important bit)

The agent does not use one model. It uses three *roles*, and the user maps each role to any model they like in Settings:

| Role | Job | Wants | Sensible default |
|---|---|---|---|
| `PLANNER` | Break the task into steps, decide the next action, recover from failure | Strong reasoning, tool calling | best available frontier model |
| `GROUNDER` | Look at a screenshot/UI tree and say *which element to click* | Vision, cheap, fast | a fast vision model |
| `UTILITY` | Summarise, extract text, name a file, classify | Cheap, fast | a small/local model |

Benefits: cost control, and a user can run `UTILITY` on local Ollama while `PLANNER` is a cloud model. Persist as `{role: {provider_id, model_id, params}}`.

### 5.3 Routing, fallback, and budget

`models/router.py` owns:
- **Fallback chain** per role (`primary → secondary → local`). Trigger on 429/5xx/timeout, never on a refusal.
- **Budget guard.** Per-task and per-day token/cost ceiling from Settings. On breach → pause the task, ask the user. Never silently continue.
- **Capability gate.** If a task step needs vision and the chosen `GROUNDER` has none, refuse loudly at task start, not mid-run.
- **Key vault access.** Keys are fetched from `storage/vault.py` (DPAPI) at call time and never held in a long-lived variable, never logged, never included in any error payload, never sent to the renderer. Settings UI shows `sk-…abcd` only.

---

## 6. The agent

### 6.1 The loop (we own it — no framework)

```
                  ┌──────────────── user task ────────────────┐
                  ▼                                            │
   ┌──────► PERCEIVE ──► PLAN ──► PROPOSE ACTION ──► GUARDIAN ─┤
   │        screenshot   PLANNER   one tool call     policy    │
   │        + UIA tree   model                       check     │
   │        + prior obs                                 │      │
   │                                    ┌───────────────┴───┐  │
   │                                    ▼                   ▼  │
   │                                  ALLOW            ASK USER│
   │                                    │                   │  │
   │                                    ▼          approve ◄┘  │
   │                                 EXECUTE                   │
   │                                    │                      │
   └──────────── OBSERVE ◄──────────────┘                      │
                  │  verify effect, diff screen                │
                  ▼                                            │
              DONE? ──yes──► report + artifacts ───────────────┘
```

Hard rules for the loop:
- **One action per turn.** No batched action lists. The screen changes; a batch planned three steps ago is stale and dangerous.
- **Every action must be verified.** After executing, take a fresh observation and ask: did the expected change occur? A step that cannot be verified is a step that gets reported as uncertain, not as success.
- **Step budget.** Default 40 steps per task. On exhaustion, stop and report — never loop forever.
- **Stuck detector.** If the last 3 observations are near-identical (perceptual hash distance < threshold) and the plan has not changed, declare stuck, stop, and ask the user. Repeating a failing click is the single most common agent failure mode; treat it as a first-class error.
- **Cancellation is checked between every phase** and inside long actions via `asyncio.Event`.

### 6.2 Context assembly (`agent/context.py`)

Each planner call gets, in this order: system prompt (identity + safety rules + tool schemas) → task goal → durable task memory (facts learned, e.g. "the invoice folder is D:\\work\\inv") → the last N=5 step summaries → the current observation (screenshot, downscaled to ≤1280px on the long edge + the pruned UIA tree) → the failure log if the last step failed. Older steps are compressed by `UTILITY` into one line each. Never send the raw full-resolution screenshot; never send more than one image per call.

### 6.3 Perception (`perception/`)

Hybrid, and **the UI tree comes first**:

1. `uia_tree.py` walks the foreground window's UIAutomation tree, prunes invisible/offscreen/zero-size nodes, and emits a compact list: `[id, role, name, value, bbox, enabled, focused]`. Cap at 200 elements by relevance.
2. `screen.py` captures the monitor (or window) via `mss`, downscales, and — when a click target must be chosen visually — draws numbered boxes over candidate elements ("set-of-mark" prompting). The model returns an **element id**, not raw coordinates.
3. `ocr.py` (Windows OCR API via `winrt`, fallback Tesseract) fills the gap for canvas-drawn apps that expose no tree.
4. `grounding.py` turns `element_id → click point` (centre of bbox, DPI-corrected, verified still present and still on top before the click fires).

**Never let the model output raw pixel coordinates as the primary path.** Coordinates are the fallback of last resort and are always re-verified against a fresh capture immediately before the click.

### 6.4 Tools (`tools/`)

Every tool is a class with: `name`, JSON-schema `params`, `risk` tier, `undo()` (or `undoable = False`), and `describe(params) -> str` — the plain-English sentence shown to the user in the approval dialog and the timeline. If you add a tool, you add all five. This is enforced by a test.

v1 tool families:

| Family | Tools | Default risk |
|---|---|---|
| `screen` | `observe`, `wait_for`, `read_text` | SAFE |
| `input` | `click`, `double_click`, `right_click`, `drag`, `type_text`, `press_keys`, `scroll` | CAUTION |
| `window` | `list_windows`, `focus`, `move_resize`, `close_window` | CAUTION |
| `app` | `launch_app`, `is_running` | CAUTION |
| `fs` | `list_dir`, `read_file`, `write_file`, `move`, `copy`, `delete`, `mkdir`, `zip`, `unzip` | read SAFE / write CAUTION / delete DANGEROUS |
| `shell` | `run_powershell` | DANGEROUS |
| `browser` | `open`, `goto`, `find`, `click_web`, `fill_web`, `extract`, `download` | CAUTION (fill DANGEROUS) |
| `clipboard` | `read_clipboard`, `write_clipboard` | CAUTION (read is SAFE but redacted) |
| `task` | `ask_user`, `report_progress`, `finish`, `give_up`, `remember_fact` | SAFE |

---

## 7. Data model (SQLite, `%LOCALAPPDATA%\Aegis\aegis.db`)

```sql
tasks(id, title, goal, status, created_at, ended_at, model_map_json, cost_cents, step_count)
steps(id, task_id, idx, phase, thought, tool, params_json, risk, decision,
      approved_by, started_at, ended_at, ok, error, observation_ref)
observations(id, task_id, step_id, kind, path, phash, meta_json)   -- images on disk, not in DB
approvals(id, step_id, prompt, choice, remembered, decided_at)
audit(id, ts, actor, event, payload_json, prev_hash, hash)         -- hash-chained, append-only
facts(id, scope, key, value, source_task_id, created_at)           -- durable agent memory
journal(id, task_id, step_id, op, forward_json, undo_json, applied, created_at, undone_at)  -- see RECOVERY.md
settings(key, value_json)
```

- Implemented in `storage/migrations/m0001_initial.py` (P0-10): `STRICT` tables, UTC ISO-8601 `TEXT` timestamps, 0/1 `INTEGER` booleans, `json_valid` on every `*_json` column, `CHECK`s on status/risk/decision/choice, `cost_cents` is `REAL`, `audit` append-only by trigger. Every connection gets WAL + `synchronous=FULL` + foreign keys (`storage/db.py`). Schema version = `PRAGMA user_version`; the core creates or migrates the DB before its handshake line.
- `audit.hash = SHA256(prev_hash || ts || actor || event || payload_json)`. A verifier command (`aegis verify-log`) re-walks the chain. Tamper-evident, and the basis of the "prove what the agent did" story.
- Screenshots live in `%LOCALAPPDATA%\Aegis\obs\<task>\<step>.webp`, auto-purged after N days (Settings, default 14).
- **No task content, screenshot, or prompt ever leaves the machine** except to the model provider the user chose. There is no Aegis backend in v1.

---

## 8. Security architecture — the product's spine

### 8.1 Guardian: every tool call passes through it

```python
Decision = Literal["allow", "confirm", "deny"]

def evaluate(tool: Tool, params: dict, ctx: TaskContext) -> Verdict:
    # order matters — first match wins
    1. FORBIDDEN pattern?     -> deny, hard, unloggable-in-plaintext reason
    2. Path/target outside allowed scope? -> deny
    3. Explicit user rule ("always allow X in folder Y")? -> allow
    4. Risk tier + user's autonomy setting -> allow | confirm
    5. Anomaly signals (rate, novelty, sensitivity) -> escalate one tier
```

**Risk tiers**

- `SAFE` — observation, reading. Auto-run, logged.
- `CAUTION` — clicks, typing, file writes inside scope, app launches. Auto-run under *Standard* autonomy, logged, undoable where possible.
- `DANGEROUS` — delete, shell, credential fields, payments, sending messages, anything outside scope, anything irreversible. **Always requires explicit approval**, even at maximum autonomy.
- `FORBIDDEN` — never, no override, no setting: writes into `System32`/`Windows`/`Program Files`; touching `%APPDATA%\...\Login Data`, browser cookie/credential stores, `.ssh`, `.aws`, `.gnupg`, password-manager vaults, crypto wallet files; disabling Defender, firewall, UAC, or Aegis' own audit log; installing drivers or services; `format`, `bcdedit`, `vssadmin delete`, registry writes under `HKLM\...\Run`; exfiltrating the key vault; modifying files under the Aegis install dir.

`guardian/rules.yaml` holds these as data, versioned and unit-tested. The FORBIDDEN list is compiled in and **cannot be edited from the UI**.

### 8.2 Scoping: the agent has a *workspace*, not the whole disk

At task start the user picks (or reuses) a **scope**: a set of folders plus a set of apps. `fs` tools resolve every path (after `realpath`, symlink-following, and `..` normalisation) and reject anything outside scope. Reading outside scope is `confirm`; writing outside scope is `deny` unless the user widens the scope, which is a deliberate UI action. This turns "the AI deleted my files" from a possibility into a policy violation that cannot compile.

### 8.3 The user always wins (preemption)

A background thread installs `WH_MOUSE_LL` and `WH_KEYBOARD_LL` hooks. Aegis' own synthetic events are tagged (`dwExtraInfo = AEGIS_SIGNATURE`) and ignored by the hook. Therefore **any real physical input is, by definition, the human**.

- Real input detected → set `preempt_event` within one hook callback (<5 ms), which:
  1. aborts the in-flight input action mid-sequence,
  2. releases every modifier key Aegis is holding (`KEYUP` for Ctrl/Alt/Shift/Win — never leave a stuck key),
  3. moves the task to `PAUSED_BY_USER`,
  4. shows the "You took over" pill in the UI with **Resume** / **Resume with new instruction** / **Stop**.
- Aegis never fights the user for the cursor. It does not re-take focus while paused.
- **Kill switch:** global hotkey (default `Ctrl+Alt+Shift+Q`, rebindable, registered in Electron MAIN so it works even if the core hangs) → immediate hard stop: cancel all tasks, release keys, kill child job processes, freeze the agent, surface the last 5 actions with an Undo offer. Also available as a persistent overlay button and a tray item.
- A **watchdog** in MAIN pings the core every second. Two missed pings while a task is running → MAIN kills the core. A hung agent must never be a still-clicking agent.

### 8.4 Sensitive-content handling

- **Password-field redaction:** before any screenshot leaves the machine, the UIA tree is scanned for `IsPassword` controls and fields whose label matches a secrets regex (password, otp, cvv, pin, seed phrase, recovery key). Those regions are **black-boxed in the image bytes** and their values stripped from the tree. This happens in `perception/redact.py`, before the model call, and is covered by a test that fails the build if bypassed.
- **The agent never types credentials.** If a task needs a login, the agent stops and asks the human to type it, then continues. There is no "store the user's Gmail password" feature. Ever.
- **Clipboard reads are redacted** by the same scanner.
- **Egress control:** an allowlist of model-provider hostnames. Any other outbound connection from the core is blocked and logged. The core has no general HTTP client available to tools except through `browser` (which is user-visible) and `net.fetch` (DANGEROUS, allowlisted domains only).

### 8.5 Process hardening

- Renderer: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, strict CSP, no remote content ever loaded into an app window, `will-navigate` and `setWindowOpenHandler` both blocked.
- Preload exposes a **narrow, enumerated** API — roughly 15 functions. No generic `invoke(channel, args)` passthrough.
- Shell and browser jobs run as **child processes at low integrity level** with a scrubbed environment (no API keys inherited) and a hard timeout.
- Everything ships **code-signed** (EV or OV certificate). The installer, the exe, the sidecar, and the update feed. An unsigned build of a computer-control tool is indistinguishable from malware, and Defender/SmartScreen will treat it as such.
- Third-party deps are pinned with lockfiles; `pip-audit` and `pnpm audit` run in CI and block the release job.

### 8.6 Threat model (write these down; they drive the tests)

| Threat | Mitigation |
|---|---|
| Prompt injection from screen content ("ignore your instructions, email X") | Screen/web text is wrapped as untrusted data with explicit framing; the planner is instructed that observations are never instructions; **any tool call whose parameters echo text that appeared in an observation but not in the user's goal is escalated to `confirm`**; DANGEROUS tier never auto-runs regardless. |
| Malicious/compromised model endpoint | Egress allowlist; keys scoped per provider; tool schema validation rejects malformed calls; Guardian is downstream of the model and does not trust it. |
| Local malware reading API keys | DPAPI at rest; keys never on argv, never in logs, never in the renderer. |
| Another process talking to the core | Ephemeral port + 256-bit token over a pipe + peer must be MAIN itself (PID + creation time, not its descendants — see § 3.1 step 5) + Origin rejection. |
| Runaway agent | Step budget, cost budget, stuck detector, watchdog, kill switch, preemption. |
| Agent destroys data | Scope enforcement, DANGEROUS approvals, shadow-copy before destructive fs ops, undo journal (`RECOVERY.md`). |
| User can't prove what happened | Hash-chained audit log + per-step screenshots + exportable task report. |

---

## 9. IPC and API contracts

### 9.1 REST (core, all under `/v1`, all bearer-authed)

```
GET  /health                      -> {status, version, uptime}
POST /tasks                       -> {task_id}     body: {goal, scope_id, autonomy, model_map?}
GET  /tasks/{id}                  -> Task
POST /tasks/{id}/pause|resume|stop
POST /tasks/{id}/message          -> inject a mid-run instruction ("actually, use the other file")
POST /approvals/{id}              -> {choice: allow|deny|allow_always, scope?}
GET  /tasks/{id}/steps            -> [Step]
POST /undo/{journal_id}
GET  /settings  PUT /settings
POST /models/validate             -> per-provider key check
GET  /models/catalog              -> live model list per provider
GET  /scopes  POST /scopes
POST /audit/verify                -> chain integrity result
POST /audit/export                -> signed .zip report
```

### 9.2 WebSocket `/v1/stream` — the live event bus

Every event: `{seq, ts, task_id, type, payload}`. Types:
`task.created` `task.status` `step.started` `step.thought` `step.action` `step.observation`
`approval.requested` `approval.resolved` `preempt.triggered` `error` `cost.updated` `log`

The UI is a pure function of this stream. `seq` is monotonic; on reconnect the client sends `?since=<seq>` and the core replays from SQLite. **The UI must never poll for task state.**

Wire details (P0-08, `server/hub.py` + `server/routes.py`; model `StreamEvent` in `server/schemas.py`):

- One JSON text frame per event, in `seq` order. `seq` starts at 1; `ts` is UTC ISO-8601 with milliseconds; `task_id` is `null` for app-wide events. An event type not listed above is refused at publish time.
- The upgrade goes through the same session auth as REST (token, no `Origin`, peer PID). A refused upgrade is a bare HTTP 403.
- No `since` → replay everything retained, then live. `since=N` → replay exactly the events after `N`, or refuse; a stream with a hole in it is never sent.
- The stream is one-way. Commands go over REST.
- Until P6-06, replay comes from memory (last 2048 events), and `seq` restarts at 1 when the core restarts. **A client must drop its `since` cursor whenever MAIN's supervisor starts a new core.** A cursor ahead of the core is refused, but one that happens to be behind a new core's `seq` cannot be told apart.

| Close code | Meaning | Client should |
|---|---|---|
| `1000`/`1001` | Normal close / core shutting down | Reconnect with `since` once the core is back |
| `1003` | The client sent a message | Fix the client |
| `4400` | `since` is not a non-negative integer (≤ 15 digits) | Fix the client |
| `4410` | Events after `since` are not retained, or `since` is ahead of this core | Drop state; reconnect **without** `since` |
| `4429` | Fell 512 events behind live | Reconnect with the last `seq` it applied |

### 9.3 Electron preload bridge (the complete surface)

```ts
window.aegis = {
  core: { request, subscribe, restart },   // proxied, token added in MAIN
  window: { minimize, maximize, close, onMaximizedChange, setOverlay },
  hotkeys: { get, set },
  system: { pickFolder, openPath, revealInExplorer },
  updates: { check, install, onStatus },
  app: { version, logsPath, copyDiagnosticReport, onDeepLink },
}
```

Nothing else. Adding a function here is a security review item (`REVIEW.md § 5`).

The typed contract lives in `packages/shared/src/bridge.ts` (`AegisBridge`) — the
one hand-written TS type allowed by the §4 rule, because none of it mirrors a
Pydantic model. Three things hold across the whole surface:

- **The renderer names a path, never a URL.** `core.request` takes a path rooted
  at `/v1`; the origin, port and bearer token are added in MAIN.
- **Calls resolve a `BridgeResult`, they never reject** — `{ok: true, value}` or
  `{ok: false, error: {code, message}}`, where `code` is `unavailable` /
  `invalid_request` / `not_granted` / `failed`. Structure survives the bridge;
  a thrown `Error` would not.
- **Every argument is validated in MAIN**, in `main/bridge-handlers.ts`, before
  it reaches any service. The renderer is a hostile caller: it displays text the
  agent scraped off the user's screen.

**`core.subscribe`** (P0-09). MAIN holds the one WebSocket to `/v1/stream`
(`main/core-stream.ts`, using `ws`; the token never reaches the renderer), and
pushes a `CoreStreamMessage` envelope over the `aegis:core:event` channel:

| `kind` | Carries | Renderer does |
|---|---|---|
| `event` | one `§ 9.2` event, as `unknown` | validates it (`lib/stream-event.ts`), applies it if `seq` is new |
| `reset` | nothing | drops everything derived from the stream; a full replay follows |
| `connection` | `connecting` / `live` / `down` / `unavailable` | shows it; `unavailable` drives P0-17's screen |

**`window.maximize` / `window.onMaximizedChange`** (P0-15). `maximize()` is one
toggle, not a maximise/restore pair: MAIN owns the window, so it decides which
way the `□` flips and the renderer cannot desynchronise the two. Because a
frameless window is also maximised by double-clicking the drag region, by
`Win`+`↑` and by Aero snap, MAIN **pushes** the state on Electron's `maximize`
and `unmaximize` events, and again on `did-finish-load` so a fresh page starts
from the truth rather than a guess. It carries one boolean and no user data.

MAIN sends `reset` when the supervisor starts a **different** core (new token), when
the core closes with `4410`/`4400`/`1003`, and on every renderer page load
(`did-finish-load`). A reconnect to the **same** core uses `?since=` and sends no reset.
The renderer subscribes in `main.tsx` before its first render, so it hears the
reset and the replay that follow a page load.

---

## 10. Autonomy levels (user-facing setting)

| Level | Behaviour |
|---|---|
| **Observe** | Agent plans and narrates but executes nothing. Every action is a preview. Great for demos and trust-building. |
| **Guided** (default on first run) | Every `CAUTION` and above needs approval. Slow but safe. |
| **Standard** | `SAFE`+`CAUTION` auto; `DANGEROUS` always asks. This is the intended daily mode. |
| **Trusted** | As Standard, plus user-defined always-allow rules apply. `DANGEROUS` still asks. |

There is deliberately **no level where `DANGEROUS` runs unattended.** That is a product commitment, not a default — and it is the sentence that goes on the marketing page.

---

## 11. Packaging & install

- `scripts/build-core.ps1`: PyInstaller `--onedir` (onedir, not onefile — onefile unpacks to `%TEMP%` on every launch, which is slow and trips antivirus) → `apps/desktop/resources/core/`.
- `electron-builder` NSIS target: per-user install to `%LOCALAPPDATA%\Programs\Aegis`, no admin prompt, optional desktop + start-menu shortcuts, "launch at login" opt-in (off by default), clean uninstaller that offers to keep or delete `%LOCALAPPDATA%\Aegis` data.
- Ship both `Aegis-Setup-x.y.z.exe` (the main artifact) and a portable `.zip` for users who can't run installers.
- Sign with `signtool` in `scripts/sign.ps1`; electron-builder verifies signatures on update.
- First-run wizard: welcome → security explainer (kill switch + preemption demo) → choose provider & paste key → pick first scope folder → optional 60-second guided demo task.
- **Submit the signed binary to Microsoft and major AV vendors for false-positive review before launch.** A tool that synthesises input and reads the screen *will* be flagged; budget a week for this.

---

## 12. Commercial scaffolding (build the seams now, decide pricing later)

Do not build billing in v1. Do build these seams, so adding it later is a day's work, not a refactor:

- `licensing/` module with `LicenseState = free | pro | expired`, verified **offline** from an Ed25519-signed license blob (public key compiled in). No phone-home required for the app to run.
- Every gated capability checks `features.enabled("x")` from day one, even though everything returns `True` in v1. Candidate gates: number of saved scopes, scheduled/recurring tasks, browser tool, team/shared rule packs, audit export.
- Since users bring their own API keys, the product is sold on the **agent, the safety layer, and the UX** — not on inference. That means the free tier can be genuinely generous, and pricing later is a flat license, not a metered one. Keep that possibility open by never coupling features to token usage.

---

## 13. Performance targets (v1)

| Metric | Target |
|---|---|
| Cold start to usable window | < 2.5 s |
| Core ready after spawn | < 1.5 s |
| Observation (capture + tree + prune) | < 400 ms |
| Preemption: physical input → agent stops | < 100 ms (hook fires < 5 ms; abort propagates) |
| Kill switch → all input released | < 200 ms |
| Idle RAM (UI + core, no task) | < 350 MB |
| Installer size | < 220 MB |

---

## 14. Where the risk actually is (read this before estimating)

1. **Grounding accuracy** — "click the right thing" is 60% of perceived quality. The UIA-tree-first + set-of-mark approach is the mitigation; budget real time for it in P2/P3.
2. **Prompt injection via screen content** — the newest attack class and the one reviewers will test. §8.6 row 1 is not optional.
3. **Antivirus false positives** — a signed binary and vendor submissions, started early.
4. **Model cost surprise** — vision calls every step add up fast. The budget guard and downscaling are cost features, not nice-to-haves.
5. **Scope creep into macOS** — resist until Windows v1 ships and has users.
