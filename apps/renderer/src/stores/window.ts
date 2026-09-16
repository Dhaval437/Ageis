import { create } from 'zustand';

/**
 * Main-window state the titlebar has to draw (P0-15).
 *
 * Today that is one boolean: is the window maximised, so the `□`/`❐` button can
 * say which one it does. Like the stream store it is **pushed, never polled**
 * (REMEMBER.md invariant 15) — MAIN owns the window, and a renderer that
 * measured the viewport instead would guess wrong the moment the user
 * double-clicked the drag region or hit `Win`+`↑`.
 */

export interface WindowSnapshot {
  readonly maximized: boolean;
}

/**
 * A window MAIN has not spoken about yet is not maximised: `createMainWindow`
 * opens it at 1100×720, and MAIN pushes the real value on every page load.
 */
export const INITIAL_WINDOW: WindowSnapshot = { maximized: false };

export interface WindowStore extends WindowSnapshot {
  readonly setMaximized: (maximized: boolean) => void;
}

export const useWindowStore = create<WindowStore>()((set) => ({
  ...INITIAL_WINDOW,
  setMaximized: (maximized) => {
    set({ maximized });
  },
}));
