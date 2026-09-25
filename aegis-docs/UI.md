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

**Type:** Inter (UI) / JetBrains Mono (code, paths, commands) — both bundled as variable woff2 (`@fontsource-variable/*`, families `Inter Variable` / `JetBrains Mono Variable`) and loaded from the app's own files, never a webfont CDN. The Windows system faces behind them in each stack are only a fallback for a file that fails to load. `fonts.test.ts` fails if either face would need the network or a `data:` URI. Scale 12 / 13 / 14 / 16 / 20 / 28. Body 14/1.5.
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
- **Engine note** (P0-09), beside the name and only while the event stream is not live: *Starting the engine…* before the first connection, *Reconnecting…* after one, and *The engine is not running.* with a red dot once the supervisor gives up. It is a `role="status"` region, and it stays while the Engine-unavailable screen (P0-17) fills the panel: the dot is the app-wide status, the screen is what to do about it.
- **Window controls** — minimise, maximise/restore and close, outside the drag region, driven through `window.aegis.window` (P0-04, maximise added in P0-15 under a `REVIEW.md § 5` review). The middle control is labelled *Maximise* with a `Square` icon, and *Restore* with a `Copy` (two overlapping squares) icon once the window is maximised — label and icon, never the icon alone. Which of the two it is comes from MAIN, pushed over `window.onMaximizedChange`, because double-clicking the drag region and `Win`+`↑` also maximise the window and the titlebar must not show a state it guessed.

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

*As built (P3-12)* — `components/ApprovalDialog.tsx`, mounted over the whole main window until its own always-on-top window exists (`P3-19`):
- It is a function of the stream (`lib/approvals.ts`): it appears on `approval.requested` and closes on `approval.resolved`, which the core sends however the question ends. One question at a time, oldest first; every payload field is validated, and one that does not parse is never shown (the core then denies it by timeout).
- **Deny is never disabled.** The input guard applies to *Allow once* and *Allow always* only, and restarts for every new question: denying is always safe, and Deny has the focus. `Esc` denies at any time.
- The heading is the fixed "Aegis needs your approval" with the tier chip (word + icon + colour); the literal action — the tool's own `describe()` sentence — sits under it in mono, clipped at 280 characters with *Show all*, and the agent's reason is quoted after `Why:`.
- The reversibility banner reads the event's `reversible` flag and shows the red *This cannot be undone* unless the core says `true`.
- The countdown is display only, computed from the event's own timestamp so it agrees with the core's timer, which is the one that denies. A failed answer is announced in an `aria-live="assertive"` line.
- `role="alertdialog"`, `aria-modal`, labelled heading, a Tab/Shift+Tab focus trap, focus returned on close, no transitions at all (the buttons override `Button`'s colour transition), and `app-no-drag` so the titlebar's drag region cannot steal a click on the dialog.

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

*As built (P3-13)* — `main/overlay.ts` + `overlay-policy.ts`, page `hud.html` / `components/OverlayHUD.tsx`:
- The two rules above conflict — a HUD that always flees the cursor can never be clicked — and invariant 1 resolves it: **while a task runs, every cursor movement is the agent's** (a human touching the mouse preempts it). So while running the HUD is fully click-through and jumps to the other edge of its screen when the cursor comes within 24 px; paused, waiting or idle it takes clicks and stays put. The person who reaches for Stop pauses the agent on the way, and finds the HUD where they aimed.
- It is **excluded from screen capture** (`setContentProtection` → `WDA_EXCLUDEFROMCAPTURE`): verified on this machine, the core's own capture of the HUD's rectangle shows the window behind it, not the HUD and not a black box.
- Frameless, transparent, always on top at `screen-saver` level, **never focusable** and shown without activation, so it cannot take keystrokes from the app the agent drives. 420×64, top-centre of the primary work area, draggable by its body.
- It shows the one-line status (connecting / offline / waiting for you / the current action / paused / stopped / ready) with amber for a question or a pause and red for a stop or an offline engine; the approval state has **Deny** and **Review** and never Allow; **■ Stop** is the kill switch; `⌃` and a double-click open the main window. There is no ⏸ button: touching the mouse *is* pause, and Resume is `P3-14`. The recent-steps strip waits for `P4-11`'s steps.
- Nothing shows it automatically yet: `window.setOverlay(true)` does, and deciding when (a task starts, the main window hides) is `P3-14`'s.

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
One card per role (`Planner`, `Grounder`, `Utility`): provider dropdown → model dropdown (from `/models/catalog`) → params (temperature, max tokens) → a **fallback chain** builder, bounded at four links because the router is. Below: **Providers** — paste a key, `Test` button showing what the provider said and how long it took, key displayed masked. A "Local (Ollama)" card auto-detects `127.0.0.1:11434` and shows installed models with a **"Nothing leaves your PC"** badge. Budget controls: per-task and per-day ceilings, with a live spend bar.

Built in `P1-10`. Four things it does that are not obvious from the sketch:

- **Edits go into a draft.** Nothing is sent until *Save changes*; *Discard* puts it back. The core is what validates — a chain that repeats a model, or an address a key may not be sent to, comes back as one sentence — so the screen does not duplicate rules the user cannot see.
- **A provider whose models cannot be listed gets a text field, not an empty dropdown.** `openrouter`, `nvidia` and a custom gateway serve catalogues no build-time table can hold, and the catalogue is built offline on purpose. A model the catalogue no longer lists keeps its value, falls back to a text field and says so, rather than rendering a blank `<select>` and losing the setting.
- **The spend bar is a function of the stream.** `GET /models/spend` gives it a first value; every one after comes from `cost.updated`. It is amber from four fifths of the ceiling and red at it, and says which in **words** as well as colour. A total missing a price reads *at least $x*.
- **A key is typed into a `password` field with autocomplete off, and is gone from the renderer the moment it is saved** — the catalogue is reloaded and what comes back is `sk-…abcd`. Nothing reads a key back; there is no route that could.

A role with no model is called out at the top of the screen, because a task cannot run until all three are mapped.

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
| Core not running | **`EngineUnavailable`** (P0-17) takes the whole conversation panel the moment the stream reports `unavailable`: the `RECOVERY.md § 4` copy verbatim, then `Restart engine` / `Open logs` / `Copy report`, then the common-causes line. Every action is disabled while one is in flight, and its outcome is announced in an `aria-live="polite"` line — failures say what to try next, and `Copy report` confirms itself because the clipboard shows nothing. `Restart engine` usually makes the screen vanish within a frame (the connection stops being `unavailable` and the titlebar says *Reconnecting…*); it comes back, with the failure notice, if that restart runs out of attempts too. |
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

How that is enforced (P0-13): every `src/components/**/*.tsx` needs a `*.stories.tsx` beside it that exports `Default`, `Loading`, `Empty`, `Error` and `LongestContent`. A state the component genuinely does not have is declared in `parameters.aegis.notApplicable` **with a reason** instead of a faked story — except `Error`, which a component in the list above may never skip. `tests/stories.test.tsx` checks all of this and renders every story, so `pnpm test` fails on a missing one. `pnpm --filter @aegis/renderer storybook` runs it on `:6006`; the toolbar switches theme.
