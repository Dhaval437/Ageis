import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, CoreResponse, CoreStreamMessage } from '@aegis/shared';
import { APPROVAL_EVENT_CHANNEL, APPROVAL_INVOKE_CHANNELS } from '../src/main/approval-channels.js';
import {
  INPUT_GUARD_MS,
  REPLAY_HOLD_MS,
  type ApprovalAnswer,
} from '../src/main/approval-policy.js';

/**
 * The approval window itself (P3-19), against a fake `electron`: how the window is
 * made, that it is on screen exactly while a question is pending, that its channel
 * answers only its own page, and that closing it denies.
 */

type Handler = (...args: unknown[]) => unknown;

const electron = vi.hoisted(() => {
  class FakeWebContents {
    readonly id: number;
    readonly send = vi.fn();
    readonly setWindowOpenHandler = vi.fn();
    readonly listeners = new Map<string, Handler>();
    constructor(id: number) {
      this.id = id;
    }
    on(name: string, handler: Handler): void {
      this.listeners.set(name, handler);
    }
  }

  class FakeBrowserWindow {
    static instances: FakeBrowserWindow[] = [];
    readonly options: Record<string, unknown>;
    readonly webContents: FakeWebContents;
    readonly listeners = new Map<string, Handler>();
    visible = false;
    focused = false;
    height = 660;
    destroyed = false;
    readonly calls: string[] = [];
    readonly setAlwaysOnTop = vi.fn();
    readonly setContentProtection = vi.fn();
    readonly loadFile = vi.fn((_path: string) => Promise.resolve());
    readonly loadURL = vi.fn((_url: string) => Promise.resolve());

    constructor(options: Record<string, unknown>) {
      this.options = options;
      this.webContents = new FakeWebContents(100 + FakeBrowserWindow.instances.length);
      FakeBrowserWindow.instances.push(this);
    }
    on(name: string, handler: Handler): void {
      this.listeners.set(name, handler);
    }
    isDestroyed(): boolean {
      return this.destroyed;
    }
    isVisible(): boolean {
      return this.visible;
    }
    isFocused(): boolean {
      return this.focused;
    }
    getBounds(): { x: number; y: number; width: number; height: number } {
      return { x: 0, y: 0, width: 480, height: this.height };
    }
    setSize(_width: number, height: number): void {
      this.height = height;
    }
    center(): void {
      this.calls.push('center');
    }
    show(): void {
      this.visible = true;
      this.calls.push('show');
    }
    moveTop(): void {
      this.calls.push('moveTop');
    }
    focus(): void {
      this.calls.push('focus');
    }
    hide(): void {
      this.visible = false;
      this.calls.push('hide');
    }
    destroy(): void {
      this.destroyed = true;
    }
  }

  const handlers = new Map<string, Handler>();
  const display = { workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  return {
    FakeBrowserWindow,
    handlers,
    display,
    screen: { getDisplayMatching: vi.fn(() => display) },
    ipcMain: {
      handle: vi.fn((channel: string, handler: Handler) => {
        handlers.set(channel, handler);
      }),
      removeHandler: vi.fn((channel: string) => {
        handlers.delete(channel);
      }),
    },
  };
});

vi.mock('electron', () => ({
  BrowserWindow: electron.FakeBrowserWindow,
  ipcMain: electron.ipcMain,
  screen: electron.screen,
}));

const { createApprovalWindow } = await import('../src/main/approval-window.js');

let seq = 0;

function event(type: string, id: number): CoreStreamMessage {
  seq += 1;
  return {
    kind: 'event',
    event: {
      seq,
      ts: '2026-09-30T12:00:00.000Z',
      task_id: '7',
      type,
      payload: { approval_id: id },
    },
  };
}
const requested = (id: number): CoreStreamMessage => event('approval.requested', id);
const resolved = (id: number): CoreStreamMessage => event('approval.resolved', id);

const ANSWERED: BridgeResult<CoreResponse> = {
  ok: true,
  value: { status: 200, body: { approval_id: 4, choice: 'allow', rule_id: null } },
};

function setup() {
  let clock = 0;
  const answer = vi.fn((_answer: ApprovalAnswer): Promise<BridgeResult<CoreResponse>> =>
    Promise.resolve(ANSWERED),
  );
  const onLoad = vi.fn();
  let foregroundWindow: number | null = 555;
  const foreground = {
    current: vi.fn(() => foregroundWindow),
    restore: vi.fn((_handle: number | bigint) => true),
  };
  const approval = createApprovalWindow({
    entry: { isPackaged: true, devServerUrl: undefined, appPath: 'C:\\App\\app.asar' },
    answer,
    onLoad,
    foreground,
    now: () => clock,
  });
  const window = electron.FakeBrowserWindow.instances.at(-1);
  if (window === undefined) throw new Error('no window was made');
  const invoke = (senderId: number, input: unknown): Promise<BridgeResult<CoreResponse>> => {
    const handler = electron.handlers.get(APPROVAL_INVOKE_CHANNELS.answer);
    if (handler === undefined) throw new Error('the answer channel is not registered');
    return handler({ sender: { id: senderId } }, input) as Promise<BridgeResult<CoreResponse>>;
  };
  return {
    approval,
    window,
    answer,
    onLoad,
    invoke,
    foreground,
    setForeground: (handle: number | null) => {
      foregroundWindow = handle;
    },
    advance: (ms: number) => {
      clock += ms;
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  electron.FakeBrowserWindow.instances.length = 0;
  electron.handlers.clear();
  electron.display.workArea.height = 1040;
  vi.clearAllMocks();
});

describe('how the approval window is made', () => {
  it('is made once, hidden, 480 wide, on top, and locked down', () => {
    const { window } = setup();
    expect(electron.FakeBrowserWindow.instances).toHaveLength(1);
    expect(window.options).toMatchObject({
      width: 480,
      show: false,
      alwaysOnTop: true,
      frame: false,
      resizable: false,
    });
    expect(window.options['webPreferences']).toMatchObject({
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      webSecurity: true,
    });
    expect(String((window.options['webPreferences'] as { preload: string }).preload)).toMatch(
      /preload[\\/]approval\.cjs$/,
    );
    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(true, 'screen-saver');
    expect(window.visible).toBe(false);
  });

  it('is excluded from screen capture: the model never sees its own Allow button', () => {
    const { window } = setup();
    expect(window.setContentProtection).toHaveBeenCalledExactlyOnceWith(true);
  });

  it('loads approval.html and cannot navigate or open windows', () => {
    const { window } = setup();
    expect(window.loadFile).toHaveBeenCalledExactlyOnceWith(
      'C:\\App\\app.asar\\renderer\\approval.html',
    );
    expect(window.webContents.setWindowOpenHandler).toHaveBeenCalledOnce();
    expect(window.webContents.listeners.has('will-navigate')).toBe(true);
  });

  it('asks for a replay when its page has loaded', () => {
    const { window, onLoad } = setup();
    window.webContents.listeners.get('did-finish-load')?.();
    expect(onLoad).toHaveBeenCalledOnce();
  });
});

describe('when the approval window is shown', () => {
  it('forwards every stream message to its page', () => {
    const { approval, window } = setup();
    const message = requested(4);
    approval.observe(message);
    approval.observe({ kind: 'connection', state: 'live' });
    expect(window.webContents.send.mock.calls).toEqual([
      [APPROVAL_EVENT_CHANNEL, message],
      [APPROVAL_EVENT_CHANNEL, { kind: 'connection', state: 'live' }],
    ]);
  });

  it('comes up centred, on top and asking for the focus when a question arrives', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    expect(window.calls).toEqual(['center', 'show', 'moveTop', 'focus']);
  });

  it('is 660 tall where the display has room, and no taller than a display that has not', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    expect(window.height).toBe(660);
    approval.observe(resolved(4));
    electron.display.workArea.height = 472;
    approval.observe(requested(5));
    expect(window.height).toBe(472);
  });

  it('hides when the question is resolved, however that happened', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe(resolved(4));
    expect(window.visible).toBe(false);
  });

  it('stays up while another question is still open, and does not jump back to the centre', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe(requested(5));
    approval.observe(resolved(4));
    expect(window.visible).toBe(true);
    expect(window.calls.filter((call) => call === 'center')).toHaveLength(1);
    approval.observe(resolved(5));
    expect(window.visible).toBe(false);
  });

  it('does not show for anything that is not a question', () => {
    const { approval, window } = setup();
    approval.observe({ kind: 'reset' });
    approval.observe({ kind: 'connection', state: 'live' });
    approval.observe(resolved(4));
    expect(window.calls).toEqual([]);
  });

  it('holds through a reset whose replay asks again', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe({ kind: 'reset' });
    expect(window.visible).toBe(true);
    approval.observe(requested(4));
    vi.advanceTimersByTime(REPLAY_HOLD_MS * 2);
    expect(window.visible).toBe(true);
  });

  it('hides after a reset whose replay does not ask again', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe({ kind: 'reset' });
    vi.advanceTimersByTime(REPLAY_HOLD_MS);
    expect(window.visible).toBe(false);
  });

  it('hides when the core is gone: nobody is waiting on the answer', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe({ kind: 'connection', state: 'unavailable' });
    expect(window.visible).toBe(false);
  });

  it('raise() brings a pending question forward, and does nothing with none', () => {
    const { approval, window } = setup();
    approval.raise();
    expect(window.calls).toEqual([]);
    approval.observe(requested(4));
    window.calls.length = 0;
    approval.raise();
    expect(window.calls).toEqual(['show', 'moveTop', 'focus']);
  });
});

describe('the focus the approval window takes', () => {
  it('goes back to the window that had it, when the dialog hides still holding it', () => {
    const { approval, window, foreground } = setup();
    approval.observe(requested(4));
    window.focused = true;
    approval.observe(resolved(4));
    expect(foreground.restore).toHaveBeenCalledExactlyOnceWith(555);
    expect(window.visible).toBe(false);
  });

  it('is handed back before the window hides', () => {
    const { approval, window, foreground } = setup();
    approval.observe(requested(4));
    window.focused = true;
    foreground.restore.mockImplementation(() => {
      expect(window.visible).toBe(true);
      return true;
    });
    approval.observe(resolved(4));
    expect(foreground.restore).toHaveBeenCalledOnce();
  });

  it('is left alone when the person has already moved to another window', () => {
    const { approval, window, foreground } = setup();
    approval.observe(requested(4));
    window.focused = false;
    approval.observe(resolved(4));
    expect(foreground.restore).not.toHaveBeenCalled();
  });

  it('remembers who had it before the first question, not between questions', () => {
    const { approval, window, foreground, setForeground } = setup();
    approval.observe(requested(4));
    setForeground(999);
    approval.observe(requested(5));
    approval.observe(resolved(4));
    window.focused = true;
    approval.observe(resolved(5));
    expect(foreground.current).toHaveBeenCalledOnce();
    expect(foreground.restore).toHaveBeenCalledExactlyOnceWith(555);
  });

  it('restores nothing when no window had the focus, and forgets between showings', () => {
    const { approval, window, foreground, setForeground } = setup();
    approval.observe(requested(4));
    window.focused = true;
    approval.observe(resolved(4));
    setForeground(null);
    approval.observe(requested(5));
    approval.observe(resolved(5));
    expect(foreground.restore).toHaveBeenCalledOnce();
  });
});

describe('answers from the approval window', () => {
  it('sends a checked answer on, and hands back what the core said', async () => {
    const { approval, window, answer, invoke, advance } = setup();
    approval.observe(requested(4));
    advance(INPUT_GUARD_MS);
    const result = await invoke(window.webContents.id, {
      approvalId: 4,
      choice: 'allow',
      rule: null,
    });
    expect(result).toBe(ANSWERED);
    expect(answer).toHaveBeenCalledExactlyOnceWith({ approvalId: 4, choice: 'allow', rule: null });
  });

  it('refuses any sender but its own page, even with a valid answer', async () => {
    const { approval, window, answer, invoke, advance } = setup();
    approval.observe(requested(4));
    advance(INPUT_GUARD_MS);
    const result = await invoke(window.webContents.id + 1, {
      approvalId: 4,
      choice: 'allow',
      rule: null,
    });
    expect(result).toEqual({
      ok: false,
      error: { code: 'invalid_request', message: 'That request did not come from the dialog.' },
    });
    expect(answer).not.toHaveBeenCalled();
  });

  it('refuses what the policy refuses, in the policy’s words, and sends nothing', async () => {
    const { approval, window, answer, invoke } = setup();
    approval.observe(requested(4));
    const early = await invoke(window.webContents.id, {
      approvalId: 4,
      choice: 'allow',
      rule: null,
    });
    const stranger = await invoke(window.webContents.id, {
      approvalId: 9,
      choice: 'deny',
      rule: null,
    });
    const malformed = await invoke(window.webContents.id, 'allow');
    for (const result of [early, stranger, malformed]) {
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error.code).toBe('invalid_request');
    }
    expect(early).toMatchObject({
      error: { message: expect.stringMatching(/too soon/) as unknown },
    });
    expect(answer).not.toHaveBeenCalled();
  });

  it('closing the window denies the question it shows, and the window stays', () => {
    const { approval, window, answer } = setup();
    approval.observe(requested(4));
    approval.observe(requested(5));
    const closing = { preventDefault: vi.fn() };
    window.listeners.get('close')?.(closing);
    expect(closing.preventDefault).toHaveBeenCalledOnce();
    expect(answer).toHaveBeenCalledExactlyOnceWith({ approvalId: 4, choice: 'deny', rule: null });
    expect(window.destroyed).toBe(false);
  });

  it('closing it with nothing pending answers nothing', () => {
    const { window, answer } = setup();
    window.listeners.get('close')?.({ preventDefault: vi.fn() });
    expect(answer).not.toHaveBeenCalled();
  });

  it('dispose destroys the window, unregisters the channel and cancels a pending hide', () => {
    const { approval, window } = setup();
    approval.observe(requested(4));
    approval.observe({ kind: 'reset' });
    approval.dispose();
    expect(window.destroyed).toBe(true);
    expect(electron.handlers.has(APPROVAL_INVOKE_CHANNELS.answer)).toBe(false);
    vi.advanceTimersByTime(REPLAY_HOLD_MS);
    expect(window.calls).not.toContain('hide');
  });
});
