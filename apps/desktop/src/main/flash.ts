/**
 * The kill switch's red edge flash (`UI.md § 7`, P3-14): one transparent window
 * per display, for `FLASH_MS`, then gone.
 *
 * Each window is as inert as a window can be: no preload, **JavaScript disabled**,
 * a `data:` page with a `default-src 'none'` policy, click-through, never
 * focusable, shown without activation and excluded from screen capture. It cannot
 * take a click or a keystroke from the person, and it never appears in the model's
 * screenshots.
 */

import { BrowserWindow, screen } from 'electron';
import { FLASH_MAX_MS, FLASH_MS, flashPageUrl } from './flash-page.js';
import { hardenNavigation } from './window.js';

function flashDisplay(bounds: Electron.Rectangle): void {
  const window = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    focusable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    show: false,
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      javascript: false,
      webviewTag: false,
      webSecurity: true,
    },
  });
  const close = (): void => {
    if (!window.isDestroyed()) window.destroy();
  };
  window.setAlwaysOnTop(true, 'screen-saver');
  window.setIgnoreMouseEvents(true);
  window.setContentProtection(true);
  hardenNavigation(window);
  window.once('ready-to-show', () => {
    if (window.isDestroyed()) return;
    window.showInactive();
    setTimeout(close, FLASH_MS);
  });
  setTimeout(close, FLASH_MAX_MS);
  window.loadURL(flashPageUrl()).catch((error: unknown) => {
    console.error('[main] the stop flash did not load', error);
    close();
  });
}

/** Flash every display's edges red. Never throws for one display failing. */
export function flashScreens(): void {
  for (const display of screen.getAllDisplays()) {
    try {
      flashDisplay(display.bounds);
    } catch (error: unknown) {
      console.error('[main] the stop flash failed on a display', error);
    }
  }
}
