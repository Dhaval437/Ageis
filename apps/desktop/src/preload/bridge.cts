/**
 * The preload bridge — the ONLY surface exposed to the renderer.
 *
 * The renderer is sandboxed with `nodeIntegration: false` and has zero fs/net
 * access (`ARCHITECTURE.md § 3`). Everything it can do is enumerated here via
 * `contextBridge.exposeInMainWorld`, one explicit method on one explicit
 * channel. There is deliberately **no generic `invoke` passthrough**: a
 * renderer that can name its own channel can reach every handler MAIN has ever
 * registered, which turns the whole IPC layer into one undocumented API.
 *
 * The surface is fixed by `ARCHITECTURE.md § 9.3`. Adding to it is a security
 * review item (`REVIEW.md § 5`).
 *
 * This file is `.cts`, not `.ts`, on purpose: a sandboxed preload cannot be an
 * ES module, so it must emit CommonJS even though the rest of MAIN is ESM
 * (`REMEMBER.md § 5`). Renaming it back would silently break the window.
 *
 * It is also **self-contained by necessity**. A sandboxed preload's `require`
 * resolves `electron` and a few builtins and nothing else — it cannot load a
 * sibling file — so the channel names are literals here rather than an import
 * from `main/bridge-channels.ts`. `tests/bridge.test.ts` drives this file with
 * a mocked `electron` and asserts the two lists are identical, so the
 * duplication cannot drift.
 */

import { contextBridge, ipcRenderer } from 'electron';
import type { IpcRendererEvent } from 'electron';
import type {
  AegisBridge,
  BridgeResult,
  CoreRequest,
  CoreStreamMessage,
  CoreResponse,
  HotkeyMap,
  Unsubscribe,
  UpdateStatus,
} from '@aegis/shared';

/**
 * Wraps a main→renderer channel.
 *
 * The `IpcRendererEvent` is dropped rather than forwarded: it carries a
 * `sender` handle, and handing that to the renderer would give it the generic
 * `send` this whole file exists to avoid.
 */
function on<T>(channel: string, listener: (payload: T) => void): Unsubscribe {
  const wrapped = (_event: IpcRendererEvent, payload: T): void => {
    listener(payload);
  };
  ipcRenderer.on(channel, wrapped);
  return () => {
    ipcRenderer.removeListener(channel, wrapped);
  };
}

const bridge: AegisBridge = {
  core: {
    request: async (request: CoreRequest): Promise<BridgeResult<CoreResponse>> =>
      (await ipcRenderer.invoke('aegis:core:request', request)) as BridgeResult<CoreResponse>,
    subscribe: (listener: (message: CoreStreamMessage) => void): Unsubscribe =>
      on<CoreStreamMessage>('aegis:core:event', listener),
  },

  window: {
    minimize: (): void => {
      ipcRenderer.send('aegis:window:minimize');
    },
    maximize: (): void => {
      ipcRenderer.send('aegis:window:maximize');
    },
    close: (): void => {
      ipcRenderer.send('aegis:window:close');
    },
    onMaximizedChange: (listener: (maximized: boolean) => void): Unsubscribe =>
      on<boolean>('aegis:window:maximized', listener),
    setOverlay: async (visible: boolean): Promise<BridgeResult<null>> =>
      (await ipcRenderer.invoke('aegis:window:set-overlay', visible)) as BridgeResult<null>,
  },

  hotkeys: {
    get: async (): Promise<BridgeResult<HotkeyMap>> =>
      (await ipcRenderer.invoke('aegis:hotkeys:get')) as BridgeResult<HotkeyMap>,
    set: async (hotkeys: HotkeyMap): Promise<BridgeResult<HotkeyMap>> =>
      (await ipcRenderer.invoke('aegis:hotkeys:set', hotkeys)) as BridgeResult<HotkeyMap>,
  },

  system: {
    pickFolder: async (): Promise<BridgeResult<string | null>> =>
      (await ipcRenderer.invoke('aegis:system:pick-folder')) as BridgeResult<string | null>,
    openPath: async (path: string): Promise<BridgeResult<null>> =>
      (await ipcRenderer.invoke('aegis:system:open-path', path)) as BridgeResult<null>,
    revealInExplorer: async (path: string): Promise<BridgeResult<null>> =>
      (await ipcRenderer.invoke('aegis:system:reveal-in-explorer', path)) as BridgeResult<null>,
  },

  updates: {
    check: async (): Promise<BridgeResult<UpdateStatus>> =>
      (await ipcRenderer.invoke('aegis:updates:check')) as BridgeResult<UpdateStatus>,
    install: async (): Promise<BridgeResult<null>> =>
      (await ipcRenderer.invoke('aegis:updates:install')) as BridgeResult<null>,
    onStatus: (listener: (status: UpdateStatus) => void): Unsubscribe =>
      on<UpdateStatus>('aegis:updates:status', listener),
  },

  app: {
    version: async (): Promise<string> => (await ipcRenderer.invoke('aegis:app:version')) as string,
    logsPath: async (): Promise<string> =>
      (await ipcRenderer.invoke('aegis:app:logs-path')) as string,
    onDeepLink: (listener: (url: string) => void): Unsubscribe =>
      on<string>('aegis:app:deep-link', listener),
  },
};

contextBridge.exposeInMainWorld('aegis', bridge);
