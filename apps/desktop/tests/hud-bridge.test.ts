import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AegisHudBridge } from '@aegis/shared';
import { ALL_CHANNELS } from '../src/main/bridge-channels.js';
import {
  HUD_CHANNELS,
  HUD_EVENT_CHANNEL,
  HUD_INVOKE_CHANNELS,
  HUD_SEND_CHANNELS,
  parseApprovalId,
} from '../src/main/hud-channels.js';
import { resolveHudEntry } from '../src/main/renderer-entry.js';

/**
 * The OverlayHUD's preload (P3-13). It is loaded for real against a mocked
 * `electron`, as `bridge.test.ts` does for the main window's, and held to two
 * things: it exposes exactly the four members `AegisHudBridge` names — nothing that
 * could allow, request or open — and it uses exactly the channels MAIN registers
 * for it, none of the main window's.
 */

type Listener = (event: unknown, payload: unknown) => void;

const exposed = vi.hoisted<{ current: { key: string; api: AegisHudBridge } | null }>(() => ({
  current: null,
}));
const ipc = vi.hoisted(() => ({
  invoke: vi.fn((_channel: string, _payload?: unknown): Promise<unknown> => Promise.resolve(null)),
  send: vi.fn((_channel: string): void => undefined),
  on: vi.fn((_channel: string, _listener: Listener): void => undefined),
  removeListener: vi.fn((_channel: string, _listener: Listener): void => undefined),
}));

vi.mock('electron', () => ({
  contextBridge: {
    exposeInMainWorld: (key: string, api: AegisHudBridge) => {
      exposed.current = { key, api };
    },
  },
  ipcRenderer: ipc,
}));

function take(): { key: string; api: AegisHudBridge } | null {
  return exposed.current;
}

async function loadHud(): Promise<{ key: string; api: AegisHudBridge }> {
  vi.resetModules();
  exposed.current = null;
  await import('../src/preload/hud.cjs');
  const loaded = take();
  if (loaded === null) throw new Error('the HUD preload exposed nothing');
  return loaded;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the HUD preload', () => {
  it('exposes window.aegisHud with exactly four members, and no way to allow', async () => {
    const { key, api } = await loadHud();
    expect(key).toBe('aegisHud');
    expect(Object.keys(api).sort()).toEqual(['deny', 'showMain', 'stop', 'subscribe']);
    expect(Object.keys(api)).not.toContain('allow');
  });

  it('uses exactly the HUD channels, and none of the main window’s', async () => {
    const { api } = await loadHud();
    const unsubscribe = api.subscribe(() => undefined);
    unsubscribe();
    api.stop();
    api.showMain();
    await api.deny(3);
    const used = [
      ...ipc.on.mock.calls.map(([channel]) => channel),
      ...ipc.send.mock.calls.map(([channel]) => channel),
      ...ipc.invoke.mock.calls.map(([channel]) => channel),
    ];
    expect([...new Set(used)].sort()).toEqual([...HUD_CHANNELS].sort());
    for (const channel of HUD_CHANNELS) expect(ALL_CHANNELS).not.toContain(channel);
    expect(ipc.invoke).toHaveBeenCalledWith(HUD_INVOKE_CHANNELS.deny, 3);
    expect(ipc.send).toHaveBeenCalledWith(HUD_SEND_CHANNELS.stop);
    expect(ipc.send).toHaveBeenCalledWith(HUD_SEND_CHANNELS.showMain);
  });

  it('hands the page the message, never the IPC event and its sender', async () => {
    const { api } = await loadHud();
    const seen: unknown[] = [];
    api.subscribe((message) => seen.push(message));
    const [channel, wrapped] = ipc.on.mock.calls[0] ?? [];
    expect(channel).toBe(HUD_EVENT_CHANNEL);
    wrapped?.({ sender: 'secret' }, { kind: 'reset' });
    expect(seen).toEqual([{ kind: 'reset' }]);
  });
});

describe('approval ids from the HUD', () => {
  it.each([1, 42, Number.MAX_SAFE_INTEGER])('accepts %s', (id) => {
    expect(parseApprovalId(id)).toBe(id);
  });

  it.each([0, -1, 1.5, Number.NaN, '3', null, undefined, {}, 2 ** 60])('refuses %s', (id) => {
    expect(parseApprovalId(id)).toBeNull();
  });
});

describe('where the HUD page is loaded from', () => {
  it('sits beside the main page, from the same build', () => {
    expect(
      resolveHudEntry({ isPackaged: true, devServerUrl: undefined, appPath: 'C:\\App\\app.asar' }),
    ).toEqual({ kind: 'file', value: 'C:\\App\\app.asar\\renderer\\hud.html' });
  });

  it('comes from the dev server in development, and never from it when packaged', () => {
    const input = { devServerUrl: 'http://localhost:5173/', appPath: 'C:\\repo\\apps\\desktop' };
    expect(resolveHudEntry({ ...input, isPackaged: false })).toEqual({
      kind: 'url',
      value: 'http://localhost:5173/hud.html',
    });
    expect(resolveHudEntry({ ...input, isPackaged: true }).kind).toBe('file');
  });
});
