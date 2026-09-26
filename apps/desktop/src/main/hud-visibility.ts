/**
 * When MAIN shows the OverlayHUD (`UI.md § 3`, `§ 6`; decided in P3-14).
 *
 * - **A task becomes active** (queued, running, paused by the person, waiting on an
 *   approval) → shown. The agent is driving the person's desktop, and the main window
 *   may be behind the very app it drives; the HUD is what stays visible.
 * - **No task is active any more** → hidden.
 * - **The kill switch fires** → shown, saying "Stopped by you", for `STOP_LINGER_MS`,
 *   then hidden unless a task is still active.
 *
 * It acts on **changes** only. Between them, the renderer's `window.setOverlay` still
 * shows or hides the HUD by hand, and nothing here undoes that until the next change.
 *
 * `electron`-free: the window and the timers are injected.
 */

/** How long "Stopped by you" stays up after a press, with no task active. */
export const STOP_LINGER_MS = 4_000;

export interface HudVisibilityOptions {
  readonly setVisible: (visible: boolean) => void;
  /** `TaskActivity.active`. */
  readonly taskActive: () => boolean;
  readonly lingerMs?: number;
  readonly setTimer?: (callback: () => void, ms: number) => unknown;
  readonly clearTimer?: (handle: unknown) => void;
}

export interface HudVisibility {
  /** Call after every stream message MAIN forwards. */
  readonly observe: () => void;
  /** The kill switch fired. */
  readonly stopped: () => void;
  readonly dispose: () => void;
}

export function createHudVisibility(options: HudVisibilityOptions): HudVisibility {
  const lingerMs = options.lingerMs ?? STOP_LINGER_MS;
  const setTimer =
    options.setTimer ?? ((callback: () => void, ms: number): unknown => setTimeout(callback, ms));
  const clearTimer =
    options.clearTimer ??
    ((handle: unknown): void => {
      clearTimeout(handle as NodeJS.Timeout);
    });
  let wasActive = false;
  let linger: unknown = null;

  function cancelLinger(): void {
    if (linger !== null) clearTimer(linger);
    linger = null;
  }

  return {
    observe: () => {
      const active = options.taskActive();
      if (active === wasActive) return;
      wasActive = active;
      if (active) {
        cancelLinger();
        options.setVisible(true);
      } else if (linger === null) {
        // A task that ends *because* of a stop must not take "Stopped by you" down early.
        options.setVisible(false);
      }
    },
    stopped: () => {
      cancelLinger();
      options.setVisible(true);
      linger = setTimer(() => {
        linger = null;
        if (!options.taskActive()) options.setVisible(false);
      }, lingerMs);
    },
    dispose: cancelLinger,
  };
}
