/**
 * Wires the bridge channels to `ipcMain`, and builds the services the handlers
 * run on.
 *
 * This file is the only place in MAIN that touches `ipcMain`, `dialog` or
 * `shell`. Everything with a decision in it lives in `bridge-handlers.ts`,
 * which is `electron`-free and therefore actually testable; this is the seam
 * where Electron gets bolted on.
 *
 * Every channel in `bridge-channels.ts` is registered here exactly once, and
 * nothing else is. There is no generic `invoke` passthrough (P0-04).
 */

import { app, dialog, ipcMain, shell } from 'electron';
import type { BrowserWindow, IpcMainInvokeEvent, WebContents } from 'electron';
import { realpath } from 'node:fs';
import { promisify } from 'node:util';
import type { UpdateStatus } from '@aegis/shared';
import { EVENT_CHANNELS, INVOKE_CHANNELS, SEND_CHANNELS } from './bridge-channels.js';
import {
  createBridgeHandlers,
  type BridgeDependencies,
  type BridgeHandlers,
  type SystemService,
  type WindowService,
} from './bridge-handlers.js';
import { createPathGrants, type PathGrants, type Realpath } from './path-grants.js';

/**
 * Follows junctions, symlinks and 8.3 short names to the real path.
 *
 * `realpath.native` rather than `realpath`: only the native one asks Windows,
 * which is what actually resolves a short name back to its long form. The
 * promises API has no `native`, hence the `promisify`.
 */
const REALPATH: Realpath = promisify(realpath.native);

export interface BridgeServices {
  /** The window the renderer lives in, or `null` while none is open. */
  readonly getWindow: () => BrowserWindow | null;
  /** Toggles the `OverlayHUD` window. Null until P3-13 builds it. */
  readonly setOverlay: ((visible: boolean) => Promise<void>) | null;
  /** The core gateway, hotkey service and update service, as they land. */
  readonly rest: Pick<BridgeDependencies, 'core' | 'hotkeys' | 'updates'>;
}

export interface BridgeIpc {
  readonly grants: PathGrants;
  /** Pushes a main→renderer event to the window, if it is still open. */
  readonly send: BridgeSender;
  /** Unregisters every channel. Call before the app quits. */
  readonly dispose: () => void;
}

export interface BridgeSender {
  coreEvent(event: unknown): void;
  updateStatus(status: UpdateStatus): void;
  deepLink(url: string): void;
}

/**
 * Registers every bridge channel.
 *
 * Aegis' own data directories are granted up front: the About screen's "open
 * the log folder" is a path the user never picks in a dialog, and it is the
 * one place `system.openPath` has to work out of the box.
 */
export function registerBridgeIpc(services: BridgeServices): BridgeIpc {
  const grants = createPathGrants(REALPATH);
  void grants.grantRoot(app.getPath('logs'));
  void grants.grantRoot(app.getPath('userData'));

  const handlers = createBridgeHandlers({
    ...services.rest,
    grants,
    window: () => windowService(services),
    system: () => systemService(services),
    appInfo: {
      version: () => app.getVersion(),
      logsPath: () => app.getPath('logs'),
    },
  });

  register(handlers, services);

  return {
    grants,
    send: createBridgeSender(services.getWindow),
    dispose: () => {
      for (const channel of Object.values(INVOKE_CHANNELS)) ipcMain.removeHandler(channel);
      for (const channel of Object.values(SEND_CHANNELS)) ipcMain.removeAllListeners(channel);
    },
  };
}

function windowService(services: BridgeServices): WindowService | null {
  const window = services.getWindow();
  if (window === null || window.isDestroyed()) return null;
  const { setOverlay } = services;
  return {
    minimize: () => {
      window.minimize();
    },
    close: () => {
      window.close();
    },
    setOverlay,
  };
}

function systemService(services: BridgeServices): SystemService | null {
  const window = services.getWindow();
  if (window === null || window.isDestroyed()) return null;
  return {
    pickFolder: async (): Promise<string | null> => {
      const result = await dialog.showOpenDialog(window, {
        // Modal to the main window so the picker cannot be lost behind it, and
        // so a second one cannot be opened on top of the first.
        properties: ['openDirectory', 'createDirectory'],
      });
      return result.canceled ? null : (result.filePaths[0] ?? null);
    },
    openPath: async (path: string): Promise<void> => {
      // `shell.openPath` resolves with a *message* on failure, not a rejection.
      const failure = await shell.openPath(path);
      if (failure !== '') throw new Error(failure);
    },
    revealInExplorer: (path: string): void => {
      shell.showItemInFolder(path);
    },
  };
}

/**
 * A renderer that is not ours must not reach these handlers. `will-navigate`
 * and the window-open handler in `window.ts` already make a foreign document
 * unreachable, but the sender check is what makes that a boundary rather than
 * a side effect of navigation policy.
 *
 * Split into a pure comparison so the refusal itself has a test: with no
 * window, or a destroyed one, there is nothing to trust and everything is
 * refused.
 */
export function isSenderTrusted(senderId: number, trustedId: number | null): boolean {
  return trustedId !== null && senderId === trustedId;
}

function trustedSenderId(services: BridgeServices): number | null {
  const window = services.getWindow();
  if (window === null || window.isDestroyed()) return null;
  return window.webContents.id;
}

function isTrustedSender(sender: WebContents, services: BridgeServices): boolean {
  return isSenderTrusted(sender.id, trustedSenderId(services));
}

function register(handlers: BridgeHandlers, services: BridgeServices): void {
  const invoke = <T>(
    channel: string,
    handler: (input: unknown) => Promise<T>,
    rejected: T,
  ): void => {
    ipcMain.handle(channel, async (event: IpcMainInvokeEvent, input: unknown): Promise<T> => {
      if (!isTrustedSender(event.sender, services)) {
        console.warn(`[main] refused ${channel} from an untrusted sender`);
        return rejected;
      }
      return handler(input);
    });
  };

  const send = (channel: string, handler: () => void): void => {
    ipcMain.on(channel, (event) => {
      if (!isTrustedSender(event.sender, services)) {
        console.warn(`[main] refused ${channel} from an untrusted sender`);
        return;
      }
      handler();
    });
  };

  const refused = {
    ok: false as const,
    error: { code: 'invalid_request' as const, message: 'That request did not come from Aegis.' },
  };

  invoke(INVOKE_CHANNELS.coreRequest, handlers.coreRequest, refused);
  invoke(INVOKE_CHANNELS.windowSetOverlay, handlers.windowSetOverlay, refused);
  invoke(INVOKE_CHANNELS.hotkeysGet, handlers.hotkeysGet, refused);
  invoke(INVOKE_CHANNELS.hotkeysSet, handlers.hotkeysSet, refused);
  invoke(INVOKE_CHANNELS.systemPickFolder, handlers.systemPickFolder, refused);
  invoke(INVOKE_CHANNELS.systemOpenPath, handlers.systemOpenPath, refused);
  invoke(INVOKE_CHANNELS.systemRevealInExplorer, handlers.systemRevealInExplorer, refused);
  invoke(INVOKE_CHANNELS.updatesCheck, handlers.updatesCheck, refused);
  invoke(INVOKE_CHANNELS.updatesInstall, handlers.updatesInstall, refused);
  invoke(INVOKE_CHANNELS.appVersion, handlers.appVersion, '');
  invoke(INVOKE_CHANNELS.appLogsPath, handlers.appLogsPath, '');

  send(SEND_CHANNELS.windowMinimize, handlers.windowMinimize);
  send(SEND_CHANNELS.windowClose, handlers.windowClose);
}

function createBridgeSender(getWindow: () => BrowserWindow | null): BridgeSender {
  const to = (channel: string, payload: unknown): void => {
    const window = getWindow();
    if (window === null || window.isDestroyed()) return;
    window.webContents.send(channel, payload);
  };

  return {
    coreEvent: (event: unknown) => {
      to(EVENT_CHANNELS.coreEvent, event);
    },
    updateStatus: (status: UpdateStatus) => {
      to(EVENT_CHANNELS.updatesStatus, status);
    },
    deepLink: (url: string) => {
      to(EVENT_CHANNELS.appDeepLink, url);
    },
  };
}
