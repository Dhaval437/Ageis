/**
 * Where the OverlayHUD sits, and when it gets out of the way (`UI.md § 6`, P3-13).
 *
 * `electron`-free and pure, so every placement rule has a test. All rectangles are
 * in Electron's screen coordinates (DIPs), the one space `screen` and
 * `BrowserWindow.getBounds()` agree on.
 *
 * The rule that shapes the rest: **while a task is running, every movement of the
 * cursor is the agent's.** A human touching the mouse preempts the agent within
 * 100 ms (REMEMBER.md invariant 1). So while running, the HUD is click-through and
 * moves out of the cursor's way — it must never be what the agent clicks — and
 * once the task is paused, waiting or idle, it holds still and takes clicks, so
 * the person who reached for Stop finds it where they aimed.
 */

export interface Rect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface Point {
  readonly x: number;
  readonly y: number;
}

export type Edge = 'top' | 'bottom';

/** `UI.md § 3`: about 380×64. A little wider, so an approval summary fits. */
export const HUD_WIDTH = 420;
export const HUD_HEIGHT = 64;

/** Gap between the HUD and the edge of the work area. */
export const HUD_INSET = 12;

/** The cursor counts as "on" the HUD this far outside it, so it moves before contact. */
export const DODGE_MARGIN = 24;

/** The HUD's rectangle at one edge of a display's work area, centred horizontally. */
export function hudBounds(workArea: Rect, edge: Edge): Rect {
  const width = Math.min(HUD_WIDTH, Math.max(0, workArea.width - 2 * HUD_INSET));
  return {
    x: Math.round(workArea.x + (workArea.width - width) / 2),
    y:
      edge === 'top'
        ? workArea.y + HUD_INSET
        : workArea.y + workArea.height - HUD_HEIGHT - HUD_INSET,
    width,
    height: HUD_HEIGHT,
  };
}

/** Whether `point` is within `margin` of `rect`. */
export function near(point: Point, rect: Rect, margin: number = DODGE_MARGIN): boolean {
  return (
    point.x >= rect.x - margin &&
    point.x < rect.x + rect.width + margin &&
    point.y >= rect.y - margin &&
    point.y < rect.y + rect.height + margin
  );
}

export interface Placement {
  readonly edge: Edge;
  readonly bounds: Rect;
  /** Whether clicks pass through the HUD to whatever is underneath. */
  readonly clickThrough: boolean;
}

/**
 * The HUD's next placement.
 *
 * Running: click-through, and if the cursor comes near it, it jumps to the other
 * edge of the same display. Not running: it takes clicks and stays put. A HUD
 * that has been dragged somewhere (`dragged`) keeps that spot until it has to
 * dodge, when it goes back to an edge.
 */
export function place(options: {
  readonly running: boolean;
  readonly cursor: Point;
  readonly workArea: Rect;
  readonly current: Placement;
}): Placement {
  const { running, cursor, workArea, current } = options;
  if (!running) return { ...current, clickThrough: false };
  if (!near(cursor, current.bounds)) return { ...current, clickThrough: true };
  const edge: Edge = current.edge === 'top' ? 'bottom' : 'top';
  return { edge, bounds: hudBounds(workArea, edge), clickThrough: true };
}
