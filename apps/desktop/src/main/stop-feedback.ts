/**
 * What the person sees when they press the kill switch (`UI.md § 7`, P3-14).
 *
 * On each completed press: a red flash at every screen edge, "Stopped by you" in the
 * HUD and the main window, and the main window brought forward. All of it runs from
 * the kill switch's `onReport` — **after** the stop has finished, never before or
 * during it. Creating a window blocks MAIN's event loop for tens of milliseconds,
 * and that loop is what runs the 100 ms acknowledgement deadline and the terminate
 * that follows it; the stop is the invariant and the flash is only its receipt.
 *
 * The stop is MAIN's own fact — a hung or terminated core cannot report it — so it
 * travels as a `stopped` message beside the core's events. A terminated core is
 * replaced, which resets the stream, so the last stop is sent again after every
 * `reset`; the renderer decides from event times whether a newer task outranks it.
 *
 * `electron`-free: the windows, the flash and the clock are injected.
 */

import type { CoreStreamMessage, KillSwitchStop } from '@aegis/shared';
import type { KillReport } from './kill-switch.js';

export interface StopFeedbackOptions {
  /** Send one message to every page that shows the stream (main window and HUD). */
  readonly deliver: (message: CoreStreamMessage) => void;
  /** The red edge flash on every display. */
  readonly flash: () => void;
  /** Bring the main window forward, so the person can see what was stopped. */
  readonly showMain: () => void;
  /** Tell the HUD's visibility a stop happened (`hud-visibility.ts`). */
  readonly onStopped?: () => void;
  readonly now?: () => Date;
}

export interface StopFeedback {
  /** The kill switch's `onReport`: one completed press. */
  readonly reported: (report: KillReport) => void;
  /** Call after forwarding each stream message, so a `reset` is followed by the stop again. */
  readonly observe: (message: CoreStreamMessage) => void;
  /** The last press, if there has been one. */
  readonly last: () => KillSwitchStop | null;
}

export function createStopFeedback(options: StopFeedbackOptions): StopFeedback {
  const now = options.now ?? ((): Date => new Date());
  let last: KillSwitchStop | null = null;

  return {
    reported: (report) => {
      last = {
        at: now().toISOString(),
        outcome: report.outcome,
        elapsedMs: Math.max(0, Math.round(report.elapsedMs)),
      };
      options.deliver({ kind: 'stopped', stop: last });
      options.onStopped?.();
      // Each is the person's receipt, and none may stop the next one running.
      for (const step of [options.flash, options.showMain]) {
        try {
          step();
        } catch (error: unknown) {
          console.error('[main] stop feedback failed', error);
        }
      }
    },
    observe: (message) => {
      if (message.kind === 'reset' && last !== null) {
        options.deliver({ kind: 'stopped', stop: last });
      }
    },
    last: () => last,
  };
}
