import { useState, type ReactElement } from 'react';
import {
  Copy,
  Cpu,
  History,
  ListTodo,
  Minus,
  ScrollText,
  Settings,
  Shield,
  Square,
  X,
  type LucideIcon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { engineStatusView } from '@/lib/engine-status';
import { cn } from '@/lib/utils';
import { useStreamStore } from '@/stores/stream';
import { useWindowStore } from '@/stores/window';

/**
 * The main-window frame from `UI.md § 4`: titlebar, 64px rail, flexible
 * conversation column, 360px live view, composer.
 *
 * The frame is P0-03; window controls are live as of P0-04, maximise/restore as
 * of P0-15. The titlebar's
 * status reads the event-stream store (P0-09); the timeline, live view and
 * composer read it too once they have events to show (P4-11).
 */

interface Section {
  readonly id: string;
  readonly label: string;
  readonly icon: LucideIcon;
  /** Empty-state copy per `UI.md § 9`. */
  readonly empty: string;
}

/** Non-empty by construction, so the default section never needs a fallback. */
const SECTIONS: readonly [Section, ...Section[]] = [
  { id: 'tasks', label: 'Tasks', icon: ListTodo, empty: 'No task is running.' },
  { id: 'history', label: 'History', icon: History, empty: 'No tasks yet.' },
  { id: 'rules', label: 'Rules', icon: Shield, empty: 'No rules yet.' },
  { id: 'models', label: 'Models', icon: Cpu, empty: 'No model is connected.' },
  { id: 'logs', label: 'Logs', icon: ScrollText, empty: 'No activity yet.' },
  { id: 'settings', label: 'Settings', icon: Settings, empty: 'Nothing to change yet.' },
];

export function AppShell(): ReactElement {
  const [activeId, setActiveId] = useState<string>(SECTIONS[0].id);
  const active = SECTIONS.find((section) => section.id === activeId) ?? SECTIONS[0];

  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <Titlebar />
      <div className="flex min-h-0 flex-1">
        <Rail activeId={active.id} onSelect={setActiveId} />
        <main
          className="flex min-w-0 flex-1 items-center justify-center border-r border-border p-6"
          aria-live="polite"
        >
          <p className="text-base text-text-dim">{active.empty}</p>
        </main>
        <LiveView />
      </div>
      <Composer />
    </div>
  );
}

/** `UI.md § 4.1`. Drag region, so a frameless window can still be moved. */
function Titlebar(): ReactElement {
  return (
    <header className="app-drag flex h-11 shrink-0 items-center gap-3 border-b border-border bg-surface-1 px-3">
      <EngineStatus />
      <div className="ml-auto flex items-center gap-2 text-sm text-text-dim">
        {/* ScopePicker and AutonomyPicker are real controls from P3 onward. */}
        <span className="app-no-drag rounded-pill border border-border px-2.5 py-1">
          Scope: none
        </span>
        <span className="app-no-drag rounded-pill border border-border px-2.5 py-1">Standard</span>
        <WindowControls />
      </div>
    </header>
  );
}

/**
 * The titlebar's status dot and, while the engine is not live, a short note.
 * The full Engine-unavailable screen (`RECOVERY.md § 4`) is P0-17.
 */
function EngineStatus(): ReactElement {
  const connection = useStreamStore((state) => state.connection);
  const hasBeenLive = useStreamStore((state) => state.hasBeenLive);
  const view = engineStatusView(connection, hasBeenLive);
  return (
    <>
      <span
        className={cn('size-2 rounded-pill', view.tone === 'error' ? 'bg-danger' : 'bg-text-dim')}
        role="img"
        aria-label={view.label}
      />
      <span className="text-base font-medium">Aegis</span>
      {view.note !== null && (
        <span className="text-sm text-text-dim" role="status">
          {view.note}
        </span>
      )}
    </>
  );
}

/**
 * The `– □ ×` of the `UI.md § 4.1` sketch. The middle button restores instead of
 * maximising once the window is maximised, and which one it is comes from MAIN
 * (P0-15) — the window can also be maximised by double-clicking the drag region
 * or by `Win`+`↑`, neither of which passes through here.
 */
function WindowControls(): ReactElement {
  const maximized = useWindowStore((state) => state.maximized);
  const control =
    'flex size-7 items-center justify-center rounded-control text-text-dim transition-colors duration-120 ease-out hover:bg-surface-2 hover:text-text';
  return (
    <div className="app-no-drag ml-1 flex items-center">
      <button
        type="button"
        aria-label="Minimise"
        onClick={() => {
          window.aegis.window.minimize();
        }}
        className={control}
      >
        <Minus className="size-4" aria-hidden />
      </button>
      <button
        type="button"
        aria-label={maximized ? 'Restore' : 'Maximise'}
        onClick={() => {
          window.aegis.window.maximize();
        }}
        className={control}
      >
        {maximized ? (
          <Copy className="size-4" aria-hidden />
        ) : (
          <Square className="size-4" aria-hidden />
        )}
      </button>
      <button
        type="button"
        aria-label="Close"
        onClick={() => {
          window.aegis.window.close();
        }}
        className="flex size-7 items-center justify-center rounded-control text-text-dim transition-colors duration-120 ease-out hover:bg-surface-2 hover:text-danger"
      >
        <X className="size-4" aria-hidden />
      </button>
    </div>
  );
}

function Rail({
  activeId,
  onSelect,
}: {
  activeId: string;
  onSelect: (id: string) => void;
}): ReactElement {
  return (
    <nav
      aria-label="Sections"
      className="flex w-16 shrink-0 flex-col gap-1 border-r border-border bg-surface-1 p-2"
    >
      {SECTIONS.map(({ id, label, icon: Icon }) => {
        const isActive = id === activeId;
        return (
          <button
            key={id}
            type="button"
            aria-current={isActive ? 'page' : undefined}
            onClick={() => {
              onSelect(id);
            }}
            className={cn(
              'flex flex-col items-center gap-1 rounded-control px-1 py-2 text-xs transition-colors duration-120 ease-out',
              isActive ? 'bg-surface-2 text-text' : 'text-text-dim hover:bg-surface-2',
            )}
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </button>
        );
      })}
    </nav>
  );
}

/** `UI.md § 8.1` + `§ 9`: with no model connected the composer is disabled, never silent. */
function Composer(): ReactElement {
  return (
    <div className="shrink-0 border-t border-border bg-surface-1 p-3">
      <div className="flex items-center gap-2">
        <input
          type="text"
          disabled
          aria-label="Ask Aegis to do something on your PC"
          placeholder="Ask Aegis to do something on your PC…"
          className="h-8 min-w-0 flex-1 rounded-control border border-border bg-surface-2 px-3 text-base text-text placeholder:text-text-dim disabled:opacity-50"
        />
        <Button variant="accent" disabled>
          Start
        </Button>
      </div>
      <p className="mt-2 text-sm text-text-dim">Connect a model to get started.</p>
    </div>
  );
}

/** `UI.md § 4.3`. Idle shows the scope summary instead of an observation. */
function LiveView(): ReactElement {
  return (
    <aside aria-label="Live view" className="w-90 shrink-0 overflow-y-auto bg-surface-1 p-4">
      <div className="flex h-40 items-center justify-center rounded-card border border-border bg-surface-2 text-sm text-text-dim">
        No observation yet
      </div>
      <p className="mt-3 text-sm text-text-dim">
        Aegis can reach nothing until you choose a scope.
      </p>
    </aside>
  );
}
