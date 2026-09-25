/**
 * The OverlayHUD window (`UI.md § 3`, `§ 6`, P3-13): the one thing on screen while
 * the agent works and the main window is out of its way.
 *
 * What it is, and why each property:
 *
 * - **Always on top, frameless, transparent, never focusable.** It floats over the
 *   apps the agent is driving, and taking focus would steal keystrokes from them.
 * - **Excluded from screen capture** (`setContentProtection`, which is
 *   `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` on Windows 10 2004+). The
 *   core captures the screen with `BitBlt` (P2-02); without this, every screenshot
 *   the model sees would have the HUD in it, over whatever it was about to click.
 * - **Placed by `overlay-policy.ts`.** While a task runs it is click-through and
 *   moves out of the cursor's way — the agent must never click it. Paused, waiting
 *   or idle, it takes clicks and holds still.
 * - **Its own narrow preload** (`preload/hud.cts`), with every channel answering only
 *   this window's `webContents`, and the main window's channels refusing it.
 */

import { BrowserWindow, ipcMain, screen } from 'electron';
import type { IpcMainEvent, IpcMainInvokeEvent } from 'electron';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { BridgeResult, CoreStreamMessage } from '@aegis/shared';
import {
  HUD_EVENT_CHANNEL,
  HUD_INVOKE_CHANNELS,
  HUD_SEND_CHANNELS,
  parseApprovalId,
} from './hud-channels.js';
import { hudBounds, place, type Placement } from './overlay-policy.js';
import { resolveHudEntry, type RendererEntryInput } from './renderer-entry.js';
import { isSenderTrusted } from './ipc.js';
import { hardenNavigation } from './window.js';

const here = dirname(fileURLToPath(import.meta.url));
export const HUD_PRELOAD_PATH = join(here, '..', 'preload', 'hud.cjs');

/** How often the placement is re-checked while the HUD is visible. */
const PLACEMENT_INTERVAL_MS = 50;

export interface OverlayOptions {
  readonly entry: RendererEntryInput;
  /** Whether a task is running right now (`TaskActivity.running`). */
  readonly taskRunning: () => boolean;
  /** The kill switch's `trigger()`. */
  readonly stop: () => void;
  readonly showMain: () => void;
  /** Deny one approval at the core. Resolves the bridge's usual result shape. */
  readonly deny: (approvalId: number) => Promise<BridgeResult<null>>;
  /** The HUD page (re)loaded: replay the stream to it, as for the main window. */
  readonly onLoad?: () => void;
}

export interface Overlay {
  readonly setVisible: (visible: boolean) => void;
  /** Forward one stream message to the HUD, if it is open. */
  readonly send: (message: CoreStreamMessage) => void;
  /** The HUD's `webContents` id, for the main window's sender check to refuse. */
  readonly webContentsId: () => number | null;
  readonly dispose: () => void;
}

const REFUSED: BridgeResult<null> = {
  ok: false,
  error: { code: 'invalid_request', message: 'That request did not come from the HUD.' },
};

export function createOverlay(options: OverlayOptions): Overlay {
  let window: BrowserWindow | null = null;
  let placement: Placement | null = null;
  let timer: NodeJS.Timeout | null = null;

  function workAreaAt(bounds: { x: number; y: number }): Electron.Rectangle {
    return screen.getDisplayNearestPoint(bounds).workArea;
  }

  function open(): BrowserWindow {
    const workArea = screen.getPrimaryDisplay().workArea;
    const bounds = hudBounds(workArea, 'top');
    placement = { edge: 'top', bounds, clickThrough: false };
    const created = new BrowserWindow({
      ...bounds,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      resizable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      // Never takes focus: the agent's keystrokes belong to the app it is driving.
      focusable: false,
      hasShadow: false,
      show: false,
      webPreferences: {
        preload: HUD_PRELOAD_PATH,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: false,
        webSecurity: true,
      },
    });
    // Above full-screen apps and the taskbar, where a status must stay visible.
    created.setAlwaysOnTop(true, 'screen-saver');
    // `WDA_EXCLUDEFROMCAPTURE`: the model's screenshots must not contain the HUD.
    created.setContentProtection(true);
    hardenNavigation(created);
    const entry = resolveHudEntry(options.entry);
    const load =
      entry.kind === 'url' ? created.loadURL(entry.value) : created.loadFile(entry.value);
    load.catch((error: unknown) => {
      console.error('[main] failed to load the HUD', error);
    });
    created.webContents.on('did-finish-load', () => {
      options.onLoad?.();
    });
    created.on('closed', () => {
      window = null;
      stopTracking();
    });
    return created;
  }

  function apply(next: Placement): void {
    if (window === null || window.isDestroyed()) return;
    if (placement === null || next.clickThrough !== placement.clickThrough) {
      window.setIgnoreMouseEvents(next.clickThrough);
    }
    if (placement === null || next.edge !== placement.edge) {
      window.setBounds(next.bounds);
    }
    placement = next;
  }

  function track(): void {
    if (window === null || window.isDestroyed() || placement === null) return;
    // The live rectangle, not the last one computed: the person may have dragged it.
    const current = { ...placement, bounds: window.getBounds() };
    apply(
      place({
        running: options.taskRunning(),
        cursor: screen.getCursorScreenPoint(),
        workArea: workAreaAt(current.bounds),
        current,
      }),
    );
  }

  function stopTracking(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  // The same tested comparison the main window's channels use, against this window.
  const isHud = (sender: Electron.WebContents): boolean =>
    isSenderTrusted(
      sender.id,
      window !== null && !window.isDestroyed() ? window.webContents.id : null,
    );

  const onStop = (event: IpcMainEvent): void => {
    if (isHud(event.sender)) options.stop();
  };
  const onShowMain = (event: IpcMainEvent): void => {
    if (isHud(event.sender)) options.showMain();
  };
  const onDeny = async (event: IpcMainInvokeEvent, input: unknown): Promise<BridgeResult<null>> => {
    if (!isHud(event.sender)) return REFUSED;
    const id = parseApprovalId(input);
    if (id === null) {
      return { ok: false, error: { code: 'invalid_request', message: 'Not an approval.' } };
    }
    return options.deny(id);
  };
  ipcMain.on(HUD_SEND_CHANNELS.stop, onStop);
  ipcMain.on(HUD_SEND_CHANNELS.showMain, onShowMain);
  ipcMain.handle(HUD_INVOKE_CHANNELS.deny, onDeny);

  return {
    setVisible: (visible) => {
      if (!visible) {
        stopTracking();
        window?.hide();
        return;
      }
      window ??= open();
      // Shown without activating it, so it never takes focus from the app below.
      window.showInactive();
      track();
      timer ??= setInterval(track, PLACEMENT_INTERVAL_MS);
    },
    send: (message) => {
      if (window !== null && !window.isDestroyed()) {
        window.webContents.send(HUD_EVENT_CHANNEL, message);
      }
    },
    webContentsId: () => (window !== null && !window.isDestroyed() ? window.webContents.id : null),
    dispose: () => {
      stopTracking();
      ipcMain.removeListener(HUD_SEND_CHANNELS.stop, onStop);
      ipcMain.removeListener(HUD_SEND_CHANNELS.showMain, onShowMain);
      ipcMain.removeHandler(HUD_INVOKE_CHANNELS.deny);
      if (window !== null && !window.isDestroyed()) window.destroy();
      window = null;
    },
  };
}
