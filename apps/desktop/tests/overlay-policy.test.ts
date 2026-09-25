import { describe, expect, it } from 'vitest';
import {
  DODGE_MARGIN,
  HUD_HEIGHT,
  HUD_INSET,
  HUD_WIDTH,
  hudBounds,
  near,
  place,
  type Placement,
  type Rect,
} from '../src/main/overlay-policy.js';

/** Where the OverlayHUD sits and when it moves (`UI.md § 6`, P3-13). */

const WORK: Rect = { x: 0, y: 0, width: 1920, height: 1040 }; // above a 40px taskbar
const SECOND: Rect = { x: -1280, y: 100, width: 1280, height: 984 }; // left, offset

function at(edge: 'top' | 'bottom', workArea: Rect = WORK): Placement {
  return { edge, bounds: hudBounds(workArea, edge), clickThrough: false };
}

describe('hudBounds', () => {
  it('centres it at the top of the work area, inset from the edge', () => {
    expect(hudBounds(WORK, 'top')).toEqual({
      x: (1920 - HUD_WIDTH) / 2,
      y: HUD_INSET,
      width: HUD_WIDTH,
      height: HUD_HEIGHT,
    });
  });

  it('puts it above the taskbar at the bottom', () => {
    const bounds = hudBounds(WORK, 'bottom');
    expect(bounds.y + bounds.height).toBe(1040 - HUD_INSET);
  });

  it('works on a monitor left of and below the primary', () => {
    const bounds = hudBounds(SECOND, 'top');
    expect(bounds.x).toBe(-1280 + (1280 - HUD_WIDTH) / 2);
    expect(bounds.y).toBe(100 + HUD_INSET);
  });

  it('narrows rather than overflow a tiny work area', () => {
    const bounds = hudBounds({ x: 0, y: 0, width: 300, height: 400 }, 'top');
    expect(bounds.width).toBe(300 - 2 * HUD_INSET);
    expect(bounds.x).toBe(HUD_INSET);
  });
});

describe('near', () => {
  const rect: Rect = { x: 100, y: 100, width: 200, height: 50 };

  it('counts the rectangle and a margin around it', () => {
    expect(near({ x: 150, y: 120 }, rect)).toBe(true);
    expect(near({ x: 100 - DODGE_MARGIN, y: 120 }, rect)).toBe(true);
    expect(near({ x: 100 - DODGE_MARGIN - 1, y: 120 }, rect)).toBe(false);
    expect(near({ x: 150, y: 150 + DODGE_MARGIN }, rect)).toBe(false);
  });
});

describe('place', () => {
  const inside = { x: WORK.width / 2, y: HUD_INSET + 10 };
  const far = { x: 10, y: 900 };

  it('while a task runs, it is click-through', () => {
    expect(place({ running: true, cursor: far, workArea: WORK, current: at('top') })).toEqual({
      ...at('top'),
      clickThrough: true,
    });
  });

  it('while a task runs, it jumps to the other edge when the cursor comes near', () => {
    const next = place({ running: true, cursor: inside, workArea: WORK, current: at('top') });
    expect(next.edge).toBe('bottom');
    expect(next.bounds).toEqual(hudBounds(WORK, 'bottom'));
    expect(near(inside, next.bounds)).toBe(false);
    const back = place({
      running: true,
      cursor: { x: WORK.width / 2, y: next.bounds.y + 5 },
      workArea: WORK,
      current: next,
    });
    expect(back.edge).toBe('top');
  });

  it('with no task running, it takes clicks and stays where it is, cursor or not', () => {
    for (const cursor of [inside, far]) {
      expect(place({ running: false, cursor, workArea: WORK, current: at('top') })).toEqual(
        at('top'),
      );
    }
  });

  it('a dragged HUD keeps its spot until it has to dodge', () => {
    const dragged: Placement = {
      edge: 'top',
      bounds: { x: 30, y: 500, width: HUD_WIDTH, height: HUD_HEIGHT },
      clickThrough: false,
    };
    expect(place({ running: true, cursor: far, workArea: WORK, current: dragged }).bounds).toEqual(
      dragged.bounds,
    );
    const dodged = place({
      running: true,
      cursor: { x: 40, y: 510 },
      workArea: WORK,
      current: dragged,
    });
    expect(dodged.bounds).toEqual(hudBounds(WORK, 'bottom'));
  });
});
