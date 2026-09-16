import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AegisBridge } from '@aegis/shared';
import {
  ALL_CHANNELS,
  EVENT_CHANNELS,
  INVOKE_CHANNELS,
  SEND_CHANNELS,
} from '../src/main/bridge-channels.js';

/**
 * The preload cannot import a sibling module — a sandboxed preload's `require`
 * resolves `electron` and nothing else — so `bridge.cts` spells its channel
 * names out itself. These tests are what stops that duplication drifting: they
 * load the real preload against a mocked `electron`, capture what it exposes,
 * and compare it to the channel list MAIN registers.
 */

interface Exposed {
  key: string;
  api: AegisBridge;
}

type Listener = (event: unknown, payload: unknown) => void;

const exposed = vi.hoisted<{ current: Exposed | null }>(() => ({ current: null }));
const ipc = vi.hoisted(() => ({
  invoke: vi.fn((_channel: string, _payload?: unknown): Promise<unknown> => Promise.resolve(null)),
  send: vi.fn((_channel: string): void => undefined),
  on: vi.fn((_channel: string, _listener: Listener): void => undefined),
  removeListener: vi.fn((_channel: string, _listener: Listener): void => undefined),
}));

vi.mock('electron', () => ({
  contextBridge: {
    exposeInMainWorld: (key: string, api: AegisBridge) => {
      exposed.current = { key, api };
    },
  },
  ipcRenderer: ipc,
}));

/**
 * Read through a call, not directly: TypeScript narrows `exposed.current` to
 * `null` at the reset below and cannot see that importing the preload refills
 * it, which would leave the result typed `never`.
 */
function takeExposed(): Exposed | null {
  return exposed.current;
}

async function loadBridge(): Promise<AegisBridge> {
  vi.resetModules();
  exposed.current = null;
  await import('../src/preload/bridge.cjs');
  const loaded = takeExposed();
  if (loaded === null) throw new Error('the preload exposed nothing');
  return loaded.api;
}

/** The surface fixed by `ARCHITECTURE.md § 9.3`. Growing it is a `REVIEW.md § 5` item. */
const SURFACE: Record<string, readonly string[]> = {
  core: ['request', 'subscribe', 'restart'],
  window: ['minimize', 'maximize', 'close', 'onMaximizedChange', 'setOverlay'],
  hotkeys: ['get', 'set'],
  system: ['pickFolder', 'openPath', 'revealInExplorer'],
  updates: ['check', 'install', 'onStatus'],
  app: ['version', 'logsPath', 'copyDiagnosticReport', 'onDeepLink'],
};

describe('the preload bridge', () => {
  let bridge: AegisBridge;

  beforeEach(async () => {
    ipc.invoke.mockClear();
    ipc.send.mockClear();
    ipc.on.mockClear();
    ipc.removeListener.mockClear();
    bridge = await loadBridge();
  });

  it('exposes itself as `window.aegis`', () => {
    expect(exposed.current?.key).toBe('aegis');
  });

  it('exposes exactly the six namespaces of ARCHITECTURE.md § 9.3, and nothing else', () => {
    expect(Object.keys(bridge).sort()).toEqual(Object.keys(SURFACE).sort());
  });

  it('exposes exactly the documented members of each namespace', () => {
    for (const [namespace, members] of Object.entries(SURFACE)) {
      const actual = bridge[namespace as keyof AegisBridge];
      expect(Object.keys(actual).sort(), namespace).toEqual([...members].sort());
    }
  });

  it('has no generic passthrough: every member is a function, none forwards a channel name', () => {
    for (const namespace of Object.values(bridge) as Record<string, unknown>[]) {
      for (const [name, member] of Object.entries(namespace)) {
        expect(typeof member, name).toBe('function');
      }
    }
    // A passthrough would have to take the channel from its caller. The
    // channel-parity test below is what actually proves it does not, since a
    // caller-supplied channel could never appear in MAIN's list.
    expect(Object.keys(bridge)).not.toContain('invoke');
  });

  it('reaches only channels MAIN registers', async () => {
    await bridge.core.request({ method: 'GET', path: '/health' });
    await bridge.window.setOverlay(true);
    await bridge.hotkeys.get();
    await bridge.hotkeys.set({ killSwitch: 'Control+Alt+Shift+Q' });
    await bridge.system.pickFolder();
    await bridge.system.openPath('C:\\x');
    await bridge.system.revealInExplorer('C:\\x');
    await bridge.updates.check();
    await bridge.updates.install();
    await bridge.app.version();
    await bridge.app.logsPath();
    await bridge.app.copyDiagnosticReport();
    await bridge.core.restart();
    bridge.window.minimize();
    bridge.window.maximize();
    bridge.window.close();
    bridge.core.subscribe(() => undefined);
    bridge.window.onMaximizedChange(() => undefined);
    bridge.updates.onStatus(() => undefined);
    bridge.app.onDeepLink(() => undefined);

    const used: string[] = [
      ...ipc.invoke.mock.calls.map(([channel]) => channel),
      ...ipc.send.mock.calls.map(([channel]) => channel),
      ...ipc.on.mock.calls.map(([channel]) => channel),
    ];

    for (const channel of used) {
      expect(ALL_CHANNELS, channel).toContain(channel);
    }
    // …and in the other direction: no channel MAIN registers is unreachable.
    expect([...new Set(used)].sort()).toEqual([...ALL_CHANNELS].sort());
  });

  it('sends the payload straight through on an invoke channel', async () => {
    await bridge.core.request({ method: 'POST', path: '/tasks', body: { goal: 'x' } });
    expect(ipc.invoke).toHaveBeenCalledWith(INVOKE_CHANNELS.coreRequest, {
      method: 'POST',
      path: '/tasks',
      body: { goal: 'x' },
    });
  });

  it('uses fire-and-forget sends for the window controls, which return nothing', () => {
    bridge.window.minimize();
    expect(ipc.send).toHaveBeenCalledWith(SEND_CHANNELS.windowMinimize);
    bridge.window.maximize();
    expect(ipc.send).toHaveBeenCalledWith(SEND_CHANNELS.windowMaximize);
    bridge.window.close();
    expect(ipc.send).toHaveBeenCalledWith(SEND_CHANNELS.windowClose);
  });

  it('carries the pushed maximised state, and nothing else, to the renderer', () => {
    const listener = vi.fn();
    const unsubscribe = bridge.window.onMaximizedChange(listener);
    const registered = ipc.on.mock.calls.find(
      ([channel]) => channel === EVENT_CHANNELS.windowMaximized,
    );
    const wrapped = registered?.[1];
    if (wrapped === undefined) throw new Error('the preload registered no listener');

    wrapped({ sender: 'the whole ipcRenderer' }, true);
    expect(listener).toHaveBeenCalledExactlyOnceWith(true);

    unsubscribe();
    expect(ipc.removeListener).toHaveBeenCalledWith(EVENT_CHANNELS.windowMaximized, wrapped);
  });

  it('never hands the renderer the IpcRendererEvent, which carries a sender handle', () => {
    const listener = vi.fn();
    bridge.core.subscribe(listener);
    const registered = ipc.on.mock.calls.find(([channel]) => channel === EVENT_CHANNELS.coreEvent);
    const wrapped = registered?.[1];
    if (wrapped === undefined) throw new Error('the preload registered no listener');

    wrapped({ sender: 'the whole ipcRenderer' }, { seq: 1 });
    expect(listener).toHaveBeenCalledExactlyOnceWith({ seq: 1 });
  });

  it('unsubscribes the exact listener it registered', () => {
    const listener = vi.fn();
    const unsubscribe = bridge.core.subscribe(listener);
    const registered = ipc.on.mock.calls[0]?.[1];
    unsubscribe();
    expect(ipc.removeListener).toHaveBeenCalledWith(EVENT_CHANNELS.coreEvent, registered);
  });
});
