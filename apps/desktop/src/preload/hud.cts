/**
 * The OverlayHUD's preload (P3-13) — `window.aegisHud`, and nothing else.
 *
 * A second, much narrower surface than `bridge.cts`, for a second window: the HUD
 * shows the same untrusted screen text the main window does and sits over the apps
 * the agent is driving, so it is given four things. Two of them only ever make Aegis
 * do less (stop, deny), one brings the main window forward, and one listens. There is
 * no `allow`, no generic core request, no file or hotkey access (`AegisHudBridge`).
 *
 * `.cts` and self-contained for the same reasons as `bridge.cts`: a sandboxed preload
 * is CommonJS and cannot require a sibling file, so the channel names are literals,
 * and `tests/hud-bridge.test.ts` holds them equal to `main/hud-channels.ts`.
 */

import { contextBridge, ipcRenderer } from 'electron';
import type { IpcRendererEvent } from 'electron';
import type { AegisHudBridge, BridgeResult, CoreStreamMessage, Unsubscribe } from '@aegis/shared';

const hud: AegisHudBridge = {
  subscribe: (listener: (message: CoreStreamMessage) => void): Unsubscribe => {
    // The event is dropped: it carries a `sender` handle the page must not hold.
    const wrapped = (_event: IpcRendererEvent, message: CoreStreamMessage): void => {
      listener(message);
    };
    ipcRenderer.on('aegis:hud:event', wrapped);
    return () => {
      ipcRenderer.removeListener('aegis:hud:event', wrapped);
    };
  },
  stop: (): void => {
    ipcRenderer.send('aegis:hud:stop');
  },
  showMain: (): void => {
    ipcRenderer.send('aegis:hud:show-main');
  },
  deny: async (approvalId: number): Promise<BridgeResult<null>> =>
    (await ipcRenderer.invoke('aegis:hud:deny', approvalId)) as BridgeResult<null>,
};

contextBridge.exposeInMainWorld('aegisHud', hud);
