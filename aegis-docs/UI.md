# UI.md

> How AEGIS looks and behaves. This file is binding: if a screen is not described here, design it to the principles in §1 and then **add it to this file in the same PR**.
> Companion: `ARCHITECTURE.md § 9.2` (the event stream that drives every pixel).

---

## 1. Principles

1. **The user must always know what is about to happen, what is happening, and what happened.** Three tenses, always visible.
2. **Trust is the UI's product.** Every design tension resolves toward legibility over density, and toward "show the user" over "hide the machinery".
3. **The stop is bigger than the start.** Cancel/stop affordances are never smaller, dimmer, or further away than the affordance that starts the action.
4. **The agent is a guest on the user's desktop.** It never steals focus, never covers what the user is doing, never blocks input.
5. **Calm, not chatty.** No fake enthusiasm, no emoji status, no "🎉 Done!". State facts.
6. **Every dangerous moment is a full stop, not a toast.** Approvals interrupt; information does not.
7. **Dark-first**, light theme fully supported, follows the OS by default.

---

## 2. Visual language

**Colour tokens** (CSS variables; both themes defined, never define a colour only inside a media query)

```
--bg          #0B0D10   surface-0, app background
--surface-1   #12151A   panels
--surface-2   #191D24   cards, inputs
--border      #252B34
--text        #E6EAF0
--text-dim    #8A94A6
--accent      #4C8DFF   primary actions, focus ring
--safe        #3DD68C   safe actions, success
--caution     #F5B547   caution tier, paused
--danger      #FF5C5C   dangerous tier, stop, destructive
--forbidden   #B14CFF   blocked-by-policy (deliberately not red — it reads as "impossible", not "failed")
```

The light theme keeps every role and every meaning; only the values change. Each
value clears 4.5:1 against its own surface, so no role is quieter in one theme
than the other.

```
--bg          #F7F8FA
--surface-1   #FFFFFF
--surface-2   #F0F2F5
--border      #D8DDE5
--text        #11151B
--text-dim    #5B6472
--accent      #2563EB
--safe        #0E8A55
--caution     #A96500   darkened amber — the bright one fails contrast on white (§10)
--danger      #D02525
--forbidden   #7A2ECC
```

Both palettes are declared as their own block in `apps/renderer/src/index.css`;
dark is the default, the OS preference picks light, and `data-theme` on `<html>`
overrides the OS in either direction. `tokens.test.ts` fails the build if a
token is dropped, renamed, or defined for only one theme.

Risk tier colour is used **consistently and only** for risk: a `--danger` element always means an irreversible action, never just "delete this chat".

**Type:** Inter (UI) / JetBrains Mono (code, paths, commands) — not bundled yet (`PROGRESS.md P0-14`), so both stacks currently fall through to the Windows system faces rather than to a webfont request. Scale 12 / 13 / 14 / 16 / 20 / 28. Body 14/1.5.
**Spacing:** 4px base; 8/12/16/24 the common steps. **Radius:** 8px cards, 6px controls, 999px pills.
**Motion:** 120 ms ease-out for state, 200 ms for panels. Respect `prefers-reduced-motion`. Nothing animates during an approval dialog — a moving dialog is a dialog people misclick.
**Iconography:** Lucide, 16/20px, 1.5 stroke.

---

## 3. Window model

| Window | Type | Purpose |
|---|---|---|
| **Main** | 1100×720 default, min 880×600, frameless with custom titlebar | Everything: chat, timeline, settings |
| **Overlay HUD** | always-on-top, click-through except its own controls, ~380×64, snapped top-centre by default, draggable, remembers position | The only thing visible while the agent works and the main window is hidden |
| **Approval dialog** | modal, always-on-top, centred, **not** click-through, 480px wide | Blocking risk decisions |
| **Tray** | icon + menu | Status, kill switch, show/hide, quit |

The HUD exists because the agent is driving the user's actual desktop — the main window would be in the way of the very apps the agent needs to click.

---

## 4. Main window layout

```
┌─────────────────────────────────────────────────────────────────────┐
│ ● Aegis            [ Scope: Work Files ▾ ]   [ Standard ▾ ]   – □ ×  │  titlebar 44px
├──────────┬──────────────────────────────────────┬───────────────────┤
│          │                                      │                   │
│  RAIL    │        CONVERSATION + TIMELINE       │   LIVE VIEW       │
│  64px    │              flexible                │   360px           │
│          │                                      │                   │
│  ▸ Tasks │  [user] Rename the invoices in...    │  ┌─────────────┐  │
│  ▸ Hist. │                                      │  │ screenshot  │  │
│  ▸ Rules │  ┌ Step 3 ─────────────── 0.8s ─┐    │  │ of the last │  │
│  ▸ Models│  │ ○ Thinking                    │    │  │ observation │  │
│  ▸ Logs  │  │   The list has 12 files; I'll │    │  │ with the    │  │
│  ▸ Set.  │  │   start with the oldest.      │    │  │ clicked el. │  │
│          │  │ ▸ click  "File name" cell     │    │  │ highlighted │  │
│          │  │   CAUTION · undoable          │    │  └─────────────┘  │
│          │  └───────────────────────────────┘    │  Explorer — inv   │
│          │                                      │  Step 3 of ~9      │
│          │                                      │  ────────────      │
│          │                                      │  ₹0.42 · 8.1k tok  │
├──────────┴──────────────────────────────────────┴───────────────────┤
│ [ Ask Aegis to do something…                    ] [scope] [▶ Start] │  composer
├─────────────────────────────────────────────────────────────────────┤
│ ⏸ Pause   ■ Stop   ⌘ Kill switch: Ctrl+Alt+Shift+Q      ● Running   │  action bar (only when a task runs)
└─────────────────────────────────────────────────────────────────────┘
```

### 4.1 Titlebar
- **Scope selector** — which folders/apps the agent may touch this task. Shows the scope name; the dropdown lists folders with a "Manage scopes…" link. Changing scope mid-task is disabled (shows why on hover).
- **Autonomy selector** — Observe / Guided / Standard / Trusted, with a one-line description under each. Changing to a *more* permissive level while a task runs requires re-confirmation.
- **Status dot**: grey idle · blue running · amber waiting-for-you · red stopped/error · purple blocked-by-policy.
- **Engine note** (P0-09), beside the name and only while the event stream is not live: *Starting the engine…* before the first connection, *Reconnecting…* after one, and *The engine is not running.* with a red dot once the supervisor gives up. It is a `role="status"` region. The full Engine-unavailable screen is P0-17.
- **Window controls** — minimise and close, outside the drag region, driven through `window.aegis.window` (P0-04). The sketch above shows a maximise button too; the bridge surface in `ARCHITECTURE.md § 9.3` has no `maximize`, and widening it is a `REVIEW.md § 5` item, so that one is tracked as `P0-15` rather than added quietly.

### 4.2 The step card (the most important component in the app)

```
┌ Step 7 ───────────────────────────────────── 1.2s ─ ⋯ ┐
│ ○ Thought    "Rows are sorted by name, not date, so    │   collapsible, dim
│               I'll click the Date column header."      │
│                                                        │
│ ▸ Action     click  ·  column header "Date modified"   │   monospace params
│              ┌────────────────────────────────────┐    │
│              │ CAUTION  ·  undoable  ·  auto-run  │    │   risk chip row
│              └────────────────────────────────────┘    │
│                                                        │
│ ✓ Result     Sort order changed. 12 rows re-ordered.   │   green / red / amber
│              [ view screenshot ]  [ undo ]             │
└────────────────────────────────────────────────────────┘
```

- Collapsed by default once complete; the **current** step is always expanded and auto-scrolled into view (unless the user has scrolled up — then show a "jump to live" pill instead of yanking them).
- Thought text streams token-by-token. This is what makes the agent feel alive and, more importantly, auditable.
- `⋯` menu: copy step as text, copy params as JSON, view raw observation, "make this always require approval", "never do this again".

### 4.3 Live view panel
Latest observation thumbnail with the target element outlined in `--accent`. Under it: the focused window title, step counter, elapsed time, running token/cost meter. Click the thumbnail → lightbox with before/after toggle. When idle, this panel shows the scope summary instead: which folders and apps are currently reachable.

---

## 5. Approval dialog — the highest-stakes screen in the product

Triggered by `approval.requested`. Blocking, always-on-top, focus-stealing (the one place we allow it).

```
┌──────────────────────────────────────────────────────────┐
│  ⚠  Aegis wants to delete 12 files                       │   --danger header
│                                                          │
│  Delete permanently                        DANGEROUS     │
│                                                          │
│  D:\work\invoices\archive\                               │   mono, scrollable
│    2024-01.pdf, 2024-02.pdf, …  (12 files, 4.2 MB)      │   [ show all ]
│                                                          │
│  Why: "The user asked to clear last year's archive        │   agent's reason,
│        after the merge completed."                        │   quoted verbatim
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │ ↩  Recoverable — files go to Aegis' trash, and   │    │   reversibility
│  │    you can undo this for 14 days.                │    │   banner (or the
│  └──────────────────────────────────────────────────┘    │   red "cannot be
│                                                          │   undone" variant)
│         [ Deny ]   [ Allow once ]   [ Allow always ▾ ]   │
│                                                          │
│  Deny in 30s ⏱                                           │   auto-deny timer
└──────────────────────────────────────────────────────────┘
```

Rules:
- **Default focus is `Deny`.** `Esc` and the timeout both deny. Nothing dangerous ever happens because the user walked away.
- **Timeout auto-denies** (30 s DANGEROUS, 60 s CAUTION), it never auto-allows.
- Show the **literal thing**, not a summary: full paths, the exact command line, the exact text about to be typed, the exact URL. Truncation gets a "show all".
- The **reversibility banner is mandatory** and only ever says "recoverable" when `undo()` genuinely exists.
- `Allow always ▾` opens a scoped choice: *this exact action* / *this tool in this folder* / *this tool for this task only*. Never a bare "allow everything forever". Every rule created here lands in the Rules screen where it can be revoked.
- A **200 ms input-guard** disables the buttons on appear, so a click the user was already making cannot approve a deletion.

---

## 6. Overlay HUD (visible while the agent works)

```
   ╭──────────────────────────────────────────────────────╮
   │ ●  Clicking "Date modified"…      ⏸    ■    ⌃        │
   ╰──────────────────────────────────────────────────────╯
        status + current action      pause stop  expand
```

- 64px tall, translucent (`backdrop-filter: blur(24px)`), rounded, subtle border, drop shadow. Click-through everywhere except the three buttons and the drag handle.
- Text is the **current action in plain English**, updated per step.
- Turns amber and grows when an approval is pending, with the approval summary and Allow/Deny inline for simple cases.
- Turns red on error/stop.
- `⌃` expands to a 3-line recent-steps strip; expands to the main window on double-click.
- **Never covers the mouse cursor's current position** — it auto-nudges to the opposite screen edge if the cursor enters its rect.

---

## 7. The preemption experience ("I take over")

The behaviour from `ARCHITECTURE.md § 8.3`, expressed in UI:

1. User touches mouse or keyboard.
2. Within 100 ms: agent stops, HUD turns amber, text becomes **"You took over — Aegis paused."**
3. HUD expands to show three buttons:
   - **Resume** — continue from the current state (agent re-observes first; it does *not* assume the screen is as it left it).
   - **Resume with a note** — inline single-line input: *"the file is actually in the 2025 folder"*. Injected as a mid-run instruction (`POST /tasks/{id}/message`).
   - **Stop**.
4. No countdown, no auto-resume. The agent waits indefinitely. The human decides when it goes again.
5. If the user types into the composer in the main window while a task runs, that is *also* preemption — the message becomes a mid-run instruction and the agent pauses to read it.

**Kill switch feedback:** on hotkey, a full-screen 400 ms red edge-flash, all input released, HUD collapses to `■ Stopped by you`, and the main window comes forward showing the last five actions with an **Undo last N** button. The user must be able to see, in one glance, what the agent had just done.

---

## 8. Other screens

### 8.1 Home / new task (idle state)
Big composer, placeholder `Ask Aegis to do something on your PC…`. Under it: three suggestion chips generated from the current scope ("Organise the files in Work Files", "Summarise the PDFs in this folder", "Fill this form from that spreadsheet"). A slim strip of the last 3 tasks with status and a re-run button. First-run only: a "Try a safe demo" card that runs a read-only task in Observe mode.

### 8.2 History
Table: task, date, duration, steps, cost, status, scope. Filter by status/date/scope. Row → full timeline replay with screenshots. Per-task actions: re-run, export report (`.zip` with steps, screenshots, and the signed audit slice), delete.

### 8.3 Rules (the trust dashboard)
Three sections, all directly editable:
- **Always allow** — user-created rules from `Allow always`. Each shows what, where, when created, times used, and a Revoke button.
- **Always ask** — tools the user has escalated.
- **Never** — the FORBIDDEN list, rendered **read-only with a lock icon** and a line of copy: *"These can't be enabled. They're built into Aegis."* Showing an unmodifiable list is the single most trust-building screen in the app.

Plus scope management: named scopes (Work Files, Downloads, Photos…), each a folder list + app list, with a "what this allows" plain-English summary.

### 8.4 Models
One card per role (`Planner`, `Grounder`, `Utility`): provider dropdown → model dropdown (live from `/models/catalog`) → params (temperature, max tokens) → a **fallback chain** builder. Below: **Providers** — add a provider, paste key, `Test` button showing latency + a real completion, key displayed masked. A "Local (Ollama)" card auto-detects `localhost:11434` and shows installed models with a **"Nothing leaves your PC"** badge. Budget controls: per-task and per-day ceilings, with a live spend bar.

### 8.5 Logs
Two tabs. **Activity** — the human-readable audit stream, searchable, exportable. **Diagnostics** — raw core logs with level filter, a "copy for bug report" button that redacts paths and keys. A `Verify log integrity` button runs the hash-chain check and shows ✓ *N entries verified, chain intact*.

### 8.6 Settings
Sections: General (theme, language, launch at login, minimise to tray) · Security (autonomy default, kill-switch hotkey, approval timeouts, screenshot retention, redaction toggles — redaction can be made *stricter*, never weaker) · Privacy (telemetry, off by default, with an explicit list of what would be sent) · Updates · About (version, licence state, log path, "Open data folder").

### 8.7 First-run wizard
Five steps, skippable only at step 5: Welcome → **How Aegis keeps you safe** (kill switch, preemption, approvals — with a live 10-second interactive demo where the user practises the hotkey) → Connect a model → Choose your first scope folder → Try a demo task. Teaching the stop before the start is the whole point of the ordering.

---

## 9. Empty, loading, and error states

| State | Treatment |
|---|---|
| No model configured | Home composer disabled with an inline card: "Connect a model to get started → Set up". Never a silent failure. |
| Core not running | Full-panel recovery card: what happened, `Restart engine` button, `Open logs`. Follows `RECOVERY.md § 4`. |
| Model error (429/timeout) | Inline in the step card, amber, with the fallback that was tried and a `Retry` button. |
| Blocked by policy | Purple step card: "Blocked — Aegis will never do this" + the rule name + link to Rules. Not framed as an error the user should fix. |
| Agent stuck | Amber card: "I've tried the same thing 3 times without progress" + last screenshot + `Give it a hint` input + `Stop`. |
| Budget exceeded | Task pauses; card shows spend, the ceiling, `Raise limit` / `Stop`. |
| No task history | Illustration + three example tasks to click. |

Loading is never a bare spinner: skeletons for lists, streaming text for thoughts, a determinate bar for anything with known length.

---

## 10. Accessibility

- Full keyboard operation. `Ctrl+K` command palette, `Ctrl+Enter` start, `Space` pause, `Esc` deny/close, `Tab` order matching visual order.
- Focus ring `2px --accent` with 2px offset, visible on every interactive element, never removed.
- Contrast ≥ 4.5:1 body, ≥ 3:1 large text and UI borders. Verify the amber `--caution` on `--surface-1` specifically; darken if it fails.
- Risk is **never colour alone** — every tier chip carries a label and an icon.
- Live regions: `aria-live="polite"` on the step stream, `aria-live="assertive"` on approvals and preemption.
- All dialogs are proper focus traps with a labelled heading.
- `prefers-reduced-motion` disables the edge-flash (replaced by a static border) and all non-essential transitions.

---

## 11. Copy rules

- Second person, present tense, no jargon: "Aegis will delete 12 files" not "Executing fs.delete with 12 targets".
- Never claim certainty the system doesn't have: "I think this is the Date column" when grounding confidence is low.
- Errors say what happened, why, and what to do next — in that order, in one sentence each.
- Never apologise more than once. Never use "Oops".
- Risk words are fixed and never softened: **delete**, **overwrite**, **send**, **pay**, **run a command**. Not "clean up", "update", "share".
- The word "safe" is only used where the Guardian tier is literally `SAFE`.

---

## 12. Component inventory (build in this order)

`StepCard` · `RiskChip` · `ApprovalDialog` · `OverlayHUD` · `Composer` · `ScopePicker` · `AutonomyPicker` · `LiveView` · `TimelineList` · `ModelRoleCard` · `ProviderCard` · `RuleRow` · `SpendMeter` · `StatusDot` · `KillSwitchBar` · `EmptyState` · `ErrorCard` · `LogTable` · `WizardShell`

Each ships with a Storybook story covering: default, loading, error, and the longest realistic content. A component without an error story is not done (`REVIEW.md § 3`).
