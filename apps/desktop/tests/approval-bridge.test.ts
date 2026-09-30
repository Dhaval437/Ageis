import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AegisApprovalBridge } from '@aegis/shared';
import { ALL_CHANNELS } from '../src/main/bridge-channels.js';
import { HUD_CHANNELS } from '../src/main/hud-channels.js';
import {
  APPROVAL_CHANNELS,
  APPROVAL_EVENT_CHANNEL,
  APPROVAL_INVOKE_CHANNELS,
} from '../src/main/approval-channels.js';
import { resolveApprovalEntry } from '../src/main/renderer-entry.js';

/**
 * The approval window's preload (P3-19). It is loaded for real against a mocked
 * `electron`, as `hud-bridge.test.ts` does for the HUD's, and held to two things: it
 * exposes exactly the two members `AegisApprovalBridge` names — no generic request,
 * nothing that opens or reads — and it uses exactly the channels MAIN registers for
 * it, none of the main window's and none of the HUD's.
 */

type Listener = (event: unknown, payload: unknown) => void;

const exposed = vi.hoisted<{ current: { key: string; api: AegisApprovalBridge } | null }>(() => ({
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
    exposeInMainWorld: (key: string, api: AegisApprovalBridge) => {
      exposed.current = { key, api };
    },
  },
  ipcRenderer: ipc,
}));

function take(): { key: string; api: AegisApprovalBridge } | null {
  return exposed.current;
}

async function loadPreload(): Promise<{ key: string; api: AegisApprovalBridge }> {
  vi.resetModules();
  exposed.current = null;
  await import('../src/preload/approval.cjs');
  const loaded = take();
  if (loaded === null) throw new Error('the approval preload exposed nothing');
  return loaded;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the approval window’s preload', () => {
  it('exposes window.aegisApproval with exactly two members', async () => {
    const { key, api } = await loadPreload();
    expect(key).toBe('aegisApproval');
    expect(Object.keys(api).sort()).toEqual(['answer', 'subscribe']);
  });

  it('uses exactly its own channels, and none of the other windows’', async () => {
    const { api } = await loadPreload();
    const unsubscribe = api.subscribe(() => undefined);
    unsubscribe();
    await api.answer(3, 'allow_always', 'exact');
    const used = [
      ...ipc.on.mock.calls.map(([channel]) => channel),
      ...ipc.send.mock.calls.map(([channel]) => channel),
      ...ipc.invoke.mock.calls.map(([channel]) => channel),
    ];
    expect([...new Set(used)].sort()).toEqual([...APPROVAL_CHANNELS].sort());
    for (const channel of APPROVAL_CHANNELS) {
      expect(ALL_CHANNELS).not.toContain(channel);
      expect(HUD_CHANNELS).not.toContain(channel);
    }
    expect(ipc.send).not.toHaveBeenCalled();
  });

  it('sends an answer as three named fields, and nothing the page could add', async () => {
    const { api } = await loadPreload();
    await api.answer(3, 'allow_always', 'tool_in_folder');
    await api.answer(4, 'deny', null);
    expect(ipc.invoke.mock.calls).toEqual([
      [
        APPROVAL_INVOKE_CHANNELS.answer,
        { approvalId: 3, choice: 'allow_always', rule: 'tool_in_folder' },
      ],
      [APPROVAL_INVOKE_CHANNELS.answer, { approvalId: 4, choice: 'deny', rule: null }],
    ]);
  });

  it('hands back what MAIN answered', async () => {
    const refusal = { ok: false, error: { code: 'invalid_request', message: 'no' } };
    ipc.invoke.mockResolvedValueOnce(refusal);
    const { api } = await loadPreload();
    expect(await api.answer(3, 'allow', null)).toEqual(refusal);
  });

  it('hands the page the message, never the IPC event and its sender', async () => {
    const { api } = await loadPreload();
    const seen: unknown[] = [];
    api.subscribe((message) => seen.push(message));
    const [channel, wrapped] = ipc.on.mock.calls[0] ?? [];
    expect(channel).toBe(APPROVAL_EVENT_CHANNEL);
    wrapped?.({ sender: 'secret' }, { kind: 'reset' });
    expect(seen).toEqual([{ kind: 'reset' }]);
  });

  it('stops listening when unsubscribed', async () => {
    const { api } = await loadPreload();
    const unsubscribe = api.subscribe(() => undefined);
    const [, wrapped] = ipc.on.mock.calls[0] ?? [];
    unsubscribe();
    expect(ipc.removeListener).toHaveBeenCalledExactlyOnceWith(APPROVAL_EVENT_CHANNEL, wrapped);
  });
});

describe('where the approval page is loaded from', () => {
  it('sits beside the main page, from the same build', () => {
    expect(
      resolveApprovalEntry({
        isPackaged: true,
        devServerUrl: undefined,
        appPath: 'C:\\App\\app.asar',
      }),
    ).toEqual({ kind: 'file', value: 'C:\\App\\app.asar\\renderer\\approval.html' });
  });

  it('comes from the dev server in development, and never from it when packaged', () => {
    const input = { devServerUrl: 'http://localhost:5173/', appPath: 'C:\\repo\\apps\\desktop' };
    expect(resolveApprovalEntry({ ...input, isPackaged: false })).toEqual({
      kind: 'url',
      value: 'http://localhost:5173/approval.html',
    });
    expect(resolveApprovalEntry({ ...input, isPackaged: true }).kind).toBe('file');
  });
});
