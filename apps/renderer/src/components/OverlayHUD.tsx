import { useState, type ReactElement } from 'react';
import { ChevronUp, Square } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { denyApproval, showMainWindow, stopAgent } from '@/lib/hud-client';
import { canStop, hudState, hudText, hudTone, type HudTone } from '@/lib/hud-state';
import { cn } from '@/lib/utils';
import { useStreamStore } from '@/stores/stream';

/**
 * The OverlayHUD (`UI.md § 6`, P3-13): one line that says what Aegis is doing, in
 * its own always-on-top window while the agent works.
 *
 * - The status is derived from the stream (`lib/hud-state.ts`) — no state of its own
 *   but whether a Deny it sent is in flight.
 * - **It can stop and it can deny, never allow.** An approval is allowed only in the
 *   dialog, behind its input guard; here there is *Deny* and *Review*, which brings
 *   the main window (and the dialog) forward.
 * - Pause is the mouse itself: a human touching it preempts the agent (invariant 1),
 *   and the HUD, which moves out of the cursor's way only while the agent runs,
 *   holds still for the click that follows. Resume arrives with `P3-14`.
 * - The window is dragged by its body; the buttons opt out of the drag region.
 */

const DOT: Record<HudTone, string> = {
  neutral: 'bg-text-dim',
  active: 'bg-accent',
  attention: 'bg-caution',
  danger: 'bg-danger',
};

const FRAME: Record<HudTone, string> = {
  neutral: 'border-border',
  active: 'border-border',
  attention: 'border-caution',
  danger: 'border-danger',
};

export function OverlayHUD(): ReactElement {
  const snapshot = useStreamStore();
  const state = hudState(snapshot);
  const tone = hudTone(state);
  const [denying, setDenying] = useState<number | null>(null);
  const approvalId = state.kind === 'approval' ? state.approval.id : null;

  async function deny(id: number): Promise<void> {
    setDenying(id);
    if (!(await denyApproval(id))) setDenying(null);
  }

  return (
    <div
      role="status"
      aria-live="assertive"
      onDoubleClick={showMainWindow}
      className={cn(
        'app-drag flex h-16 w-full items-center gap-3 rounded-card border bg-surface-1/90 px-4 text-text backdrop-blur-xl',
        FRAME[tone],
      )}
    >
      <span aria-hidden className={cn('size-2.5 shrink-0 rounded-pill', DOT[tone])} />
      <p
        className={cn(
          'min-w-0 flex-1 truncate text-base',
          tone === 'attention' && 'text-caution',
          tone === 'danger' && 'text-danger',
        )}
        title={hudText(state)}
      >
        {hudText(state)}
      </p>

      <div className="app-no-drag flex shrink-0 items-center gap-1">
        {approvalId !== null && (
          <>
            <Button
              size="sm"
              variant="secondary"
              className="transition-none"
              disabled={denying === approvalId}
              onClick={() => {
                void deny(approvalId);
              }}
            >
              {denying === approvalId ? 'Denying…' : 'Deny'}
            </Button>
            <Button size="sm" variant="ghost" className="transition-none" onClick={showMainWindow}>
              Review
            </Button>
          </>
        )}
        {canStop(state) && (
          <Button
            size="icon"
            variant="ghost"
            className="text-danger transition-none"
            aria-label="Stop"
            title="Stop — the same as the kill switch"
            onClick={stopAgent}
          >
            <Square aria-hidden />
          </Button>
        )}
        <Button
          size="icon"
          variant="ghost"
          className="transition-none"
          aria-label="Open Aegis"
          title="Open Aegis"
          onClick={showMainWindow}
        >
          <ChevronUp aria-hidden />
        </Button>
      </div>
    </div>
  );
}
