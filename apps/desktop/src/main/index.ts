/**
 * Electron MAIN entry point.
 *
 * MAIN is the only process the user sees, and it owns the things that must keep
 * working when everything else is wedged: the kill-switch hotkey (REMEMBER.md
 * invariant 2) and the supervision of the Python core (invariant 14).
 *
 * Landed so far: window, frameless shell for the custom titlebar, tray, the
 * single-instance lock (P0-02), the preload bridge (P0-04), the core
 * supervisor (P0-07) and the event stream forwarded to the renderer (P0-09). The real kill switch is P3-06 — the bridge namespaces it
 * backs are registered and answer `unavailable` until then.
 */

import { app } from 'electron';
import type { BrowserWindow } from 'electron';
import { join } from 'node:path';
import { createMainWindow } from './window.js';
import { createTray, type TrayHandle } from './tray.js';
import type { TrayMenuState } from './tray-menu.js';
import { registerBridgeIpc, type BridgeIpc } from './ipc.js';
import { createCoreStream, type CoreAvailability, type CoreStream } from './core-stream.js';
import {
  createSupervisor,
  resolveCoreLaunch,
  type Supervisor,
  type SupervisorState,
} from './supervisor.js';

/** Must match `appId` in `electron-builder.yml` or Windows gives us a second taskbar identity. */
const APP_USER_MODEL_ID = 'dev.aegis.app';

let mainWindow: BrowserWindow | null = null;
let tray: TrayHandle | null = null;
let bridge: BridgeIpc | null = null;
let supervisor: Supervisor | null = null;
let coreStream: CoreStream | null = null;
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
  // Fires on the first load and on every reload: whatever the page had been told
  // died with it, so the stream starts over and replays what the core retains.
  mainWindow.webContents.on('did-finish-load', () => {
    coreStream?.restart();
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
    coreStream?.dispose();
    coreStream = null;
    bridge?.dispose();
    bridge = null;
    // Invariant 14: the core must not outlive the UI. `stop()` is fire-and-
    // forget here because Electron will not wait for a promise on this event —
    // it kills the child synchronously and only the confirmation is async. The
    // core's own parent watch covers the paths MAIN never gets to run.
    void supervisor?.stop();
    supervisor = null;
  });

  app
    .whenReady()
    .then(() => {
      // Registered before the window exists, so the renderer cannot call a
      // channel that is not there yet during its first paint.
      coreStream = createCoreStream({
        deliver: (message) => {
          bridge?.send.coreEvent(message);
        },
      });
      supervisor = createCoreSupervisor();

      bridge = registerBridgeIpc({
        getWindow: () => mainWindow,
        // The OverlayHUD window is P3-13; hotkeys are P3-06 and updates are
        // P7-04. Until each lands its namespace answers `unavailable` rather
        // than silently doing nothing.
        setOverlay: null,
        rest: {
          // Null while the core is down, which is how the renderer learns to
          // show "Reconnecting…" instead of a request that never returns.
          core: () => supervisor?.gateway() ?? null,
          hotkeys: () => null,
          updates: () => null,
        },
      });

      openMainWindow();
      // Not awaited: the window must paint while the core starts, and the
      // supervisor reports its own state through `onState`.
      void supervisor.start();
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

/**
 * Build the supervisor for this session.
 *
 * `AEGIS_CORE_COMMAND` points the app at an interpreter chosen by hand; without
 * it, development uses the venv in `core/` and a packaged build uses the
 * PyInstaller bundle in `resources/`.
 */
function createCoreSupervisor(): Supervisor {
  return createSupervisor({
    spec: resolveCoreLaunch({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      // `app.getAppPath()` is `apps/desktop` in development; the repo root is
      // two levels up, and that is where `core/` lives.
      projectRoot: join(app.getAppPath(), '..', '..'),
      overrideCommand: process.env['AEGIS_CORE_COMMAND'],
    }),
    onState: onSupervisorState,
    onSession: (session) => {
      coreStream?.setSession(session);
    },
  });
}

function availabilityOf(state: SupervisorState): CoreAvailability {
  if (state.status === 'running') return 'running';
  return state.status === 'unavailable' ? 'unavailable' : 'down';
}

function onSupervisorState(state: SupervisorState): void {
  coreStream?.setAvailability(availabilityOf(state));
  if (state.status === 'unavailable') {
    // The renderer now hears `unavailable` over the stream; RECOVERY.md § 4's
    // Engine-unavailable screen that acts on it is P0-17.
    console.error('[main] the core is unavailable:', state.lastError ?? 'unknown reason');
  }
}

bootstrap();
