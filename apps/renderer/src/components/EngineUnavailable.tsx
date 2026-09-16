import type { ReactElement } from 'react';
import { ClipboardCopy, FolderOpen, RotateCw } from 'lucide-react';
import type { BridgeResult } from '@aegis/shared';
import { Button } from '@/components/ui/button';
import { recoveryNotice, type RecoveryAction } from '@/lib/engine-recovery';
import { cn } from '@/lib/utils';
import { useRecoveryStore } from '@/stores/recovery';

/**
 * `RECOVERY.md § 4`'s Engine-unavailable screen, shown in place of the
 * conversation column once the supervisor has given up (P0-17).
 *
 * The three actions are the ones in that sketch and nothing else: restart the
 * engine, open the logs, copy a redacted report. None of them takes a
 * parameter — this screen asks MAIN to do a fixed thing, it never says what to
 * run or which file to read.
 *
 * It does not offer to retry by itself. A core that failed three times is not
 * going to succeed on a fourth nobody asked for, and a silent respawn loop is
 * how a hung engine becomes a process nobody can see.
 *
 * `Restart engine` usually takes this screen away within a frame: the supervisor
 * reports a core again, the connection stops being `unavailable`, and the
 * titlebar says *Reconnecting…* while it comes up. That is invariant 15 doing
 * its job — the screen is a function of the stream, not of its own button — and
 * it returns, with the failure notice, if the restart runs out of attempts too.
 */

/** Copy is verbatim from `RECOVERY.md § 4`; `UI.md § 11` forbids softening it. */
const ACTIONS: readonly { id: RecoveryAction; label: string; busyLabel: string }[] = [
  { id: 'restart', label: 'Restart engine', busyLabel: 'Starting…' },
  { id: 'logs', label: 'Open logs', busyLabel: 'Opening…' },
  { id: 'report', label: 'Copy report', busyLabel: 'Copying…' },
];

const ICONS: Record<RecoveryAction, typeof RotateCw> = {
  restart: RotateCw,
  logs: FolderOpen,
  report: ClipboardCopy,
};

async function openLogs(): Promise<BridgeResult<unknown>> {
  const path = await window.aegis.app.logsPath();
  if (path === '') {
    return { ok: false, error: { code: 'failed', message: 'No log folder.' } };
  }
  return window.aegis.system.openPath(path);
}

function runAction(action: RecoveryAction): Promise<BridgeResult<unknown>> {
  switch (action) {
    case 'restart':
      return window.aegis.core.restart();
    case 'logs':
      return openLogs();
    case 'report':
      return window.aegis.app.copyDiagnosticReport();
  }
}

export function EngineUnavailable(): ReactElement {
  const busy = useRecoveryStore((state) => state.busy);
  const notice = useRecoveryStore((state) => state.notice);
  const begin = useRecoveryStore((state) => state.begin);
  const settle = useRecoveryStore((state) => state.settle);

  async function onClick(action: RecoveryAction): Promise<void> {
    begin(action);
    try {
      settle(recoveryNotice(action, await runAction(action)));
    } catch {
      // The bridge resolves rather than rejects, so this is a renderer bug —
      // but a button left disabled forever is worse than the failure it hides.
      settle({ tone: 'error', text: 'Aegis could not run that. Close Aegis and open it again.' });
    }
  }

  return (
    <section
      aria-labelledby="engine-unavailable-title"
      className="flex h-full w-full items-center justify-center overflow-y-auto p-6"
    >
      <div className="w-full max-w-160 rounded-card border border-border bg-surface-1 p-6">
        <h1 id="engine-unavailable-title" className="text-lg font-medium text-text">
          The Aegis engine isn&rsquo;t running
        </h1>
        <p className="mt-2 text-base text-text-dim">
          It stopped 3 times in a row. Your data is safe and your task history is intact.
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          {ACTIONS.map(({ id, label, busyLabel }) => {
            const Icon = ICONS[id];
            return (
              <Button
                key={id}
                variant={id === 'restart' ? 'accent' : 'secondary'}
                disabled={busy !== null}
                onClick={() => {
                  void onClick(id);
                }}
              >
                <Icon aria-hidden />
                {busy === id ? busyLabel : label}
              </Button>
            );
          })}
        </div>

        <p aria-live="polite" className="mt-3 min-h-5 text-sm">
          {notice !== null && (
            <span className={cn(notice.tone === 'error' ? 'text-danger' : 'text-text-dim')}>
              {notice.text}
            </span>
          )}
        </p>

        <p className="mt-5 border-t border-border pt-4 text-sm text-text-dim">
          Common causes: antivirus blocked aegis-core.exe, or another program is using the
          automation APIs.
        </p>
      </div>
    </section>
  );
}
