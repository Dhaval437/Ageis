/**
 * The main window (`UI.md § 3`): 1100×720, min 880×600, frameless with a custom
 * titlebar drawn by the renderer.
 *
 * The renderer is a hostile-input surface — it renders screen text the agent
 * scraped from the user's desktop — so it gets the most locked-down
 * `webPreferences` Electron offers, and cannot navigate anywhere.
 */

import { BrowserWindow } from 'electron';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { resolveRendererEntry, type RendererEntryInput } from './renderer-entry.js';

/** `--bg` from `UI.md § 2`. Painted before the renderer loads, so no white flash. */
const BACKGROUND = '#0B0D10';

const DEFAULT_WIDTH = 1100;
const DEFAULT_HEIGHT = 720;
const MIN_WIDTH = 880;
const MIN_HEIGHT = 600;

const here = dirname(fileURLToPath(import.meta.url));

/**
 * The preload is emitted as CommonJS (`bridge.cts` → `bridge.cjs`) because a
 * sandboxed preload cannot be an ES module. See `REMEMBER.md § 5`.
 */
export const PRELOAD_PATH = join(here, '..', 'preload', 'bridge.cjs');

export function createMainWindow(entryInput: RendererEntryInput): BrowserWindow {
  const window = new BrowserWindow({
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    backgroundColor: BACKGROUND,
    // Frameless: the titlebar in `UI.md § 4.1` is drawn by the renderer (P0-03).
    frame: false,
    // Shown on `ready-to-show` instead, so the user never sees an empty frame.
    show: false,
    webPreferences: {
      preload: PRELOAD_PATH,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      // The renderer talks only to MAIN (ARCHITECTURE.md § 3).
      webSecurity: true,
    },
  });

  window.once('ready-to-show', () => {
    window.show();
  });

  hardenNavigation(window);

  const entry = resolveRendererEntry(entryInput);
  const load = entry.kind === 'url' ? window.loadURL(entry.value) : window.loadFile(entry.value);
  load.catch((error: unknown) => {
    console.error(`[main] failed to load the renderer from ${entry.value}`, error);
  });

  return window;
}

/**
 * Nothing in this window may navigate or spawn a window. Every outbound link
 * belongs to the explicit `system` namespace on the preload bridge (P0-04), not
 * to whatever a page decides to do.
 */
export function hardenNavigation(window: BrowserWindow): void {
  window.webContents.setWindowOpenHandler(({ url }) => {
    console.warn(`[main] blocked window.open to ${url}`);
    return { action: 'deny' };
  });

  window.webContents.on('will-navigate', (event, url) => {
    event.preventDefault();
    console.warn(`[main] blocked navigation to ${url}`);
  });

  window.webContents.on('will-attach-webview', (event) => {
    event.preventDefault();
  });
}
