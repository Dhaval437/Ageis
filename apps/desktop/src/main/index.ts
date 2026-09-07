/**
 * Electron MAIN entry point.
 *
 * MAIN is the only process the user sees, and it owns the things that must keep
 * working when everything else is wedged: the kill-switch hotkey (REMEMBER.md
 * invariant 2) and the supervision of the Python core (invariant 14).
 *
 * P0-02 scope: window, frameless shell for the custom titlebar, tray, and the
 * single-instance lock. The preload surface is P0-04, the core supervisor is
 * P0-06/P0-07, and the real kill switch is P3-06.
 */

import { app } from 'electron';
import type { BrowserWindow } from 'electron';
import { createMainWindow } from './window.js';
import { createTray, type TrayHandle } from './tray.js';
import type { TrayMenuState } from './tray-menu.js';

/** Must match `appId` in `electron-builder.yml` or Windows gives us a second taskbar identity. */
const APP_USER_MODEL_ID = 'dev.aegis.app';

let mainWindow: BrowserWindow | null = null;
let tray: TrayHandle | null = null;
let trayState: TrayMenuState = { windowVisible: false, taskRunning: false };

function setTrayState(patch: Partial<TrayMenuState>): void {
  trayState = { ...trayState, ...patch };
  tray?.refresh(trayState);
}

function showMainWindow(): void {
  if (mainWindow === null) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  setTrayState({ windowVisible: true });
}

function toggleMainWindow(): void {
  if (mainWindow === null) return;
  if (mainWindow.isVisible() && !mainWindow.isMinimized()) {
    mainWindow.hide();
    setTrayState({ windowVisible: false });
    return;
  }
  showMainWindow();
}

function openMainWindow(): void {
  mainWindow = createMainWindow({
    isPackaged: app.isPackaged,
    devServerUrl: process.env['AEGIS_RENDERER_URL'],
    appPath: app.getAppPath(),
  });

  mainWindow.on('show', () => {
    setTrayState({ windowVisible: true });
  });
  mainWindow.on('hide', () => {
    setTrayState({ windowVisible: false });
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
    setTrayState({ windowVisible: false });
  });
}

function bootstrap(): void {
  // Two Aegis processes means two agents with a mouse. Take the lock before
  // anything else exists, and let the first instance take the focus.
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }

  app.setAppUserModelId(APP_USER_MODEL_ID);

  app.on('second-instance', () => {
    showMainWindow();
  });

  app.on('window-all-closed', () => {
    // Windows-only for v1, and closing the window ends the session: an Aegis
    // that lives on with no window is the shape of invariant 14's failure mode.
    app.quit();
  });

  app.on('before-quit', () => {
    tray?.destroy();
    tray = null;
  });

  app
    .whenReady()
    .then(() => {
      openMainWindow();
      tray = createTray(trayState, {
        toggleWindow: toggleMainWindow,
        stopAgent: () => {
          // Unreachable until P3-06: the item is disabled while no task runs.
          console.warn('[main] stop requested, but the kill switch lands in P3-06');
        },
        quit: () => {
          app.quit();
        },
      });
    })
    .catch((error: unknown) => {
      console.error('[main] startup failed', error);
      app.quit();
    });
}

bootstrap();
