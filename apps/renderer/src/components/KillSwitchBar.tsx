import type { ReactElement } from 'react';
import { OctagonX, TriangleAlert } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { currentStop, stopDetail } from '@/lib/stop';
import { useKillSwitchStore } from '@/stores/kill-switch';
import { useStreamStore } from '@/stores/stream';

/**
 * The kill switch, as the main window shows it (`UI.md § 7`, § 12's `KillSwitchBar`;
 * P3-14). A bar at the top of the conversation panel that is there only when it has
 * something to say:
 *
 * - **"Stopped by you"** after a press, with what the stop did, until a task goes
 *   again or the person closes it. MAIN brings this window forward as it arrives.
 * - **The shortcut isn't working** when another program owns it — a kill switch that
 *   silently does nothing is the one failure the person must hear about.
 *
 * The last five actions and *Undo last N* belong here too, once there are steps to
 * list (`P4-11`) and a journal to undo from (`P6-03`).
 */
export function KillSwitchBar(): ReactElement | null {
  const stop = currentStop(useStreamStore());
  const armed = useKillSwitchStore((state) => state.armed);
  const dismissedAt = useKillSwitchStore((state) => state.dismissedAt);
  const dismiss = useKillSwitchStore((state) => state.dismiss);
  const showStop = stop !== null && stop.at !== dismissedAt;

  if (!showStop && armed.kind !== 'unarmed') return null;

  return (
    <div className="flex w-full flex-col gap-2 border-b border-border p-3">
      {armed.kind === 'unarmed' && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-card border border-caution bg-surface-2 p-3"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-caution" aria-hidden />
          <div className="min-w-0 text-base">
            <p className="font-medium text-text">The stop shortcut isn’t working.</p>
            <p className="text-text-dim">
              {armed.message} Close that program and restart Aegis. Until then, stop Aegis from its
              tray icon.
            </p>
          </div>
        </div>
      )}
      {showStop && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-card border border-danger bg-surface-2 p-3"
        >
          <OctagonX className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
          <div className="min-w-0 flex-1 text-base">
            <p className="font-medium text-danger">Stopped by you.</p>
            <p className="text-text-dim">{stopDetail(stop)}</p>
          </div>
          <Button
            size="sm"
            variant="ghost"
            className="app-no-drag shrink-0"
            onClick={() => {
              dismiss(stop.at);
            }}
          >
            Dismiss
          </Button>
        </div>
      )}
    </div>
  );
}
