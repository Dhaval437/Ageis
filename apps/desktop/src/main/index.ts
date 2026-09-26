/**
 * Electron MAIN entry point.
 *
 * MAIN is the only process the user sees, and it owns the things that must keep
 * working when everything else is wedged: the kill-switch hotkey (REMEMBER.md
 * invariant 2) and the supervision of the Python core (invariant 14).
 *
 * Landed so far: window, frameless shell for the custom titlebar, tray, the
 * single-instance lock (P0-02), the preload bridge (P0-04), the core
 * supervisor (P0-07), the event stream forwarded to the renderer (P0-09), the
 * kill switch (P3-06), the watchdog (P3-07) and the OverlayHUD (P3-13).
 */

import { app, globalShortcut } from 'electron';
import type { BrowserWindow } from 'electron';
import type { BridgeResult } from '@aegis/shared';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createMainWindow } from './window.js';
import { createTray, type TrayHandle } from './tray.js';
import type { TrayMenuState } from './tray-menu.js';
import {
  DEFAULT_KILL_SWITCH,
  createFileHotkeyStore,
  createHotkeyService,
  type KillSwitchHotkeys,
} from './hotkeys.js';
import { createKillSwitch, type KillReport, type KillSwitch } from './kill-switch.js';
import { hotkeysFile } from './paths.js';
import { registerBridgeIpc, type BridgeIpc } from './ipc.js';
import { createCoreStream, type CoreAvailability, type CoreStream } from './core-stream.js';
import { createTaskActivity } from './task-activity.js';
import { createWatchdog, type Watchdog, type WatchdogReport } from './watchdog.js';
import { createOverlay, type Overlay } from './overlay.js';
import { describeRelease, releaseModifiers } from './modifier-release.js';
import { nativeKeyInput } from './win-input.js';
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
let killSwitch: KillSwitch | null = null;
let watchdog: Watchdog | null = null;
let overlay: Overlay | null = null;
/** Whether a task is running, learnt from the stream MAIN forwards (P3-07). */
const taskActivity = createTaskActivity();
let hotkeys: KillSwitchHotkeys | null = null;
let trayState: TrayMenuState = {
  windowVisible: false,
  taskRunning: false,
  killSwitch: DEFAULT_KILL_SWITCH,
};

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
  const window = createMainWindow({
    isPackaged: app.isPackaged,
    devServerUrl: process.env['AEGIS_RENDERER_URL'],
    appPath: app.getAppPath(),
  });
  mainWindow = window;

  mainWindow.on('show', () => {
    setTrayState({ windowVisible: true });
  });
  mainWindow.on('hide', () => {
    setTrayState({ windowVisible: false });
  });
  // The `□` button is not the only way the window gets maximised — a
  // double-click on the drag region, `Win`+`↑` and Aero snap all do it — so the
  // titlebar is told about the state rather than left to assume it (P0-15).
  mainWindow.on('maximize', () => {
    bridge?.send.windowMaximized(true);
  });
  mainWindow.on('unmaximize', () => {
    bridge?.send.windowMaximized(false);
  });
  // Fires on the first load and on every reload: whatever the page had been told
  // died with it, so the stream starts over and replays what the core retains,
  // and the fresh page is told the window state it cannot have seen change.
  mainWindow.webContents.on('did-finish-load', () => {
    coreStream?.restart();
    bridge?.send.windowMaximized(window.isMaximized());
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

  app.on('will-quit', () => {
    // Electron's own rule: global shortcuts are released before the app exits,
    // or Windows keeps the combination reserved for a process that is gone.
    hotkeys?.dispose();
    hotkeys = null;
    globalShortcut.unregisterAll();
  });

  app.on('before-quit', () => {
    // First, so a core being stopped on purpose is never mistaken for a hung one.
    watchdog?.stop();
    watchdog = null;
    overlay?.dispose();
    overlay = null;
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
          taskActivity.observe(message);
          bridge?.send.coreEvent(message);
          overlay?.send(message);
        },
      });
      supervisor = createCoreSupervisor();

      // Invariant 2: armed before the window exists and before the core has
      // answered, so the stop works from the first frame — including while the
      // core is still starting, or never starts at all.
      killSwitch = createKillSwitch({
        gateway: () => supervisor?.gateway() ?? null,
        terminate: () => supervisor?.terminate('kill-switch') ?? Promise.resolve(false),
        onReport: logKillReport,
        releaseModifiers: releaseHeldModifiers,
      });
      // The other half of "a hung agent is never a still-clicking agent": the
      // kill switch covers a hang somebody notices, this one a hang nobody does.
      watchdog = createWatchdog({
        gateway: () => supervisor?.gateway() ?? null,
        taskRunning: taskActivity.running,
        terminate: () => supervisor?.terminate('watchdog') ?? Promise.resolve(false),
        onReport: logWatchdogReport,
      });
      watchdog.start();

      // `UI.md § 6`. Created on first show; its own narrow preload and channels.
      overlay = createOverlay({
        entry: {
          isPackaged: app.isPackaged,
          devServerUrl: process.env['AEGIS_RENDERER_URL'],
          appPath: app.getAppPath(),
        },
        taskRunning: taskActivity.running,
        stop: () => {
          void killSwitch?.trigger();
        },
        showMain: showMainWindow,
        deny: denyApproval,
        onLoad: () => {
          coreStream?.restart();
        },
      });
      hotkeys = createHotkeyService({
        registry: globalShortcut,
        store: createFileHotkeyStore(hotkeysFile(process.env, homedir())),
        onKillSwitch: () => {
          void killSwitch?.trigger();
        },
        onChange: (map) => {
          setTrayState({ killSwitch: map.killSwitch });
        },
      });
      hotkeys.arm();

      bridge = registerBridgeIpc({
        getWindow: () => mainWindow,
        // The OverlayHUD (P3-13). Updates are P7-04, and until then that
        // namespace answers `unavailable` rather than silently doing nothing.
        setOverlay: (visible: boolean): Promise<void> => {
          overlay?.setVisible(visible);
          return Promise.resolve();
        },
        rest: {
          // Null while the core is down, which is how the renderer learns to
          // show "Reconnecting…" instead of a request that never returns.
          core: () => supervisor?.gateway() ?? null,
          // `Restart engine` on the Engine-unavailable screen (P0-17).
          coreControl: () =>
            supervisor === null
              ? null
              : {
                  restart: async (): Promise<void> => {
                    const state = await supervisor?.restart();
                    // Resolving on a core that never came up would tell the
                    // Engine-unavailable screen the restart worked while it is
                    // still looking at a dead engine.
                    if (state?.status !== 'running') {
                      throw new Error('The engine did not start.');
                    }
                  },
                },
          hotkeys: () => hotkeys,
          updates: () => null,
        },
        engineState: () => {
          const state = supervisor?.state() ?? null;
          return {
            status: state?.status ?? 'stopped',
            attempts: state?.attempts ?? 0,
            lastError: state?.lastError ?? null,
          };
        },
      });

      openMainWindow();
      // Not awaited: the window must paint while the core starts, and the
      // supervisor reports its own state through `onState`.
      void supervisor.start();
      tray = createTray(trayState, {
        toggleWindow: toggleMainWindow,
        stopAgent: () => {
          void killSwitch?.trigger();
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
    releaseModifiers: releaseHeldModifiers,
    taskRunning: taskActivity.running,
  });
}

/**
 * One line per press, and never anything from the core's body beyond a count.
 * The renderer's "Stopped by you" is P3-14's; this is for the log a user sends.
 */
function logKillReport(report: KillReport): void {
  const elapsed = report.elapsedMs.toFixed(1);
  console.warn(`[main] kill switch: ${report.outcome} in ${elapsed} ms`);
  if (report.releaseFailures > 0) {
    console.error(
      `[main] kill switch: the core could not release ${String(report.releaseFailures)} controller(s); a key may be held`,
    );
  }
}

/**
 * `RECOVERY.md § 4`: a dead or hung core cannot let go of the keys it held, so MAIN
 * does, through its own keyboard (P3-15). Synchronous, so nothing runs before it.
 */
function releaseHeldModifiers(reason: string): void {
  const report = releaseModifiers(nativeKeyInput());
  if (report.released.length > 0 || report.failed.length > 0 || report.unavailable) {
    const line = describeRelease(reason, report);
    if (report.failed.length > 0 || report.unavailable) console.error(line);
    else console.warn(line);
  }
}

/**
 * The HUD's *Deny* (P3-13): `POST /v1/approvals/{id}` with `deny`, and nothing else —
 * the HUD cannot allow. A 404 means the question was already closed, which is fine.
 */
async function denyApproval(approvalId: number): Promise<BridgeResult<null>> {
  const gateway = supervisor?.gateway() ?? null;
  if (gateway === null) {
    return { ok: false, error: { code: 'unavailable', message: 'The engine is not running.' } };
  }
  try {
    const response = await gateway.request({
      method: 'POST',
      path: `/approvals/${String(approvalId)}`,
      body: { choice: 'deny' },
    });
    if (response.status === 200 || response.status === 404) return { ok: true, value: null };
    return { ok: false, error: { code: 'failed', message: 'The engine refused the answer.' } };
  } catch {
    return { ok: false, error: { code: 'failed', message: 'The engine did not answer.' } };
  }
}

/** One line per kill; the numbers are all MAIN has, and all a bug report needs. */
function logWatchdogReport(report: WatchdogReport): void {
  const silent =
    report.silentMs === null ? 'never answered' : `silent ${report.silentMs.toFixed(0)} ms`;
  console.error(
    `[main] watchdog: the core missed ${String(report.misses)} pings during a task (${silent}); ${report.outcome}`,
  );
}

function availabilityOf(state: SupervisorState): CoreAvailability {
  if (state.status === 'running') return 'running';
  return state.status === 'unavailable' ? 'unavailable' : 'down';
}

function onSupervisorState(state: SupervisorState): void {
  coreStream?.setAvailability(availabilityOf(state));
  if (state.status === 'unavailable') {
    // The renderer hears `unavailable` over the stream and shows RECOVERY.md
    // § 4's Engine-unavailable screen, whose `Restart engine` comes back here
    // through `core.restart` (P0-17).
    console.error('[main] the core is unavailable:', state.lastError ?? 'unknown reason');
  }
}

bootstrap();
