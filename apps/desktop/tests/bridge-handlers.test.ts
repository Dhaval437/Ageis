import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HotkeyMap } from '@aegis/shared';
import {
  createBridgeHandlers,
  parseCoreRequest,
  parseHotkeyMap,
  type BridgeDependencies,
  type BridgeHandlers,
  type CoreGateway,
  type HotkeyService,
  type SystemService,
  type UpdateService,
  type WindowService,
} from '../src/main/bridge-handlers.js';
import { createPathGrants } from '../src/main/path-grants.js';

const WORK = 'C:\\Users\\Bo\\Work';

function services() {
  const core: CoreGateway = {
    request: vi.fn(() => Promise.resolve({ status: 200, body: { ok: true } })),
  };
  let maximized = false;
  const window: WindowService = {
    minimize: vi.fn(),
    isMaximized: vi.fn(() => maximized),
    setMaximized: vi.fn((next: boolean) => {
      maximized = next;
    }),
    close: vi.fn(),
    setOverlay: vi.fn(() => Promise.resolve()),
  };
  const hotkeys: HotkeyService = {
    get: vi.fn(() => ({ killSwitch: 'Control+Alt+Shift+Q' })),
    set: vi.fn((map: HotkeyMap) => Promise.resolve(map)),
  };
  const system: SystemService = {
    pickFolder: vi.fn((): Promise<string | null> => Promise.resolve(WORK)),
    openPath: vi.fn(() => Promise.resolve()),
    revealInExplorer: vi.fn(),
  };
  const updates: UpdateService = {
    check: vi.fn(() => Promise.resolve({ state: 'idle' as const, version: null, message: null })),
    install: vi.fn(() => Promise.resolve()),
  };
  return { core, window, hotkeys, system, updates };
}

/** `realpath` for a world where everything under `WORK` exists and resolves to itself. */
const realpath = (path: string): Promise<string> => {
  if (path.toLowerCase().startsWith(WORK.toLowerCase())) return Promise.resolve(path);
  if (path.toLowerCase() === 'c:\\logs') return Promise.resolve('C:\\Logs');
  return Promise.reject(new Error('ENOENT'));
};

function build(overrides: Partial<BridgeDependencies> = {}): {
  handlers: BridgeHandlers;
  svc: ReturnType<typeof services>;
} {
  const svc = services();
  const deps: BridgeDependencies = {
    core: () => svc.core,
    window: () => svc.window,
    hotkeys: () => svc.hotkeys,
    system: () => svc.system,
    updates: () => svc.updates,
    grants: createPathGrants(realpath),
    appInfo: { version: () => '0.0.0', logsPath: () => 'C:\\Logs' },
    ...overrides,
  };
  return { handlers: createBridgeHandlers(deps), svc };
}

/** Every service absent — the shape MAIN actually boots in until P0-06/P3-06/P7-04. */
function buildEmpty(): BridgeHandlers {
  return createBridgeHandlers({
    core: () => null,
    window: () => null,
    hotkeys: () => null,
    system: () => null,
    updates: () => null,
    grants: createPathGrants(realpath),
    appInfo: { version: () => '0.0.0', logsPath: () => 'C:\\Logs' },
  });
}

describe('parseCoreRequest', () => {
  it('accepts a plain rooted path', () => {
    expect(parseCoreRequest({ method: 'GET', path: '/tasks/abc/steps' })).toEqual({
      method: 'GET',
      path: '/tasks/abc/steps',
      body: undefined,
    });
  });

  it('keeps a query string', () => {
    expect(parseCoreRequest({ method: 'GET', path: '/stream?since=12' })?.path).toBe(
      '/stream?since=12',
    );
  });

  it('refuses anything that could re-target the request away from the core', () => {
    const rejected = [
      { method: 'GET', path: 'http://evil.example/steal' },
      { method: 'GET', path: '//evil.example/steal' },
      { method: 'GET', path: '/\\evil.example/steal' },
      { method: 'GET', path: '/tasks\\..\\admin' },
      { method: 'GET', path: '/tasks/../../admin' },
      { method: 'GET', path: 'tasks' },
      { method: 'GET', path: '' },
      { method: 'GET', path: `/tasks/${'a'.repeat(4096)}` },
    ];
    for (const input of rejected) {
      expect(parseCoreRequest(input), input.path).toBeNull();
    }
  });

  it('refuses control characters in the path', () => {
    expect(parseCoreRequest({ method: 'GET', path: '/tasks\r\nX-Evil: 1' })).toBeNull();
    expect(parseCoreRequest({ method: 'GET', path: '/tasks\u0000' })).toBeNull();
  });

  it('refuses a method that is not one of the four the core exposes', () => {
    for (const method of ['PATCH', 'CONNECT', 'get', '', 42]) {
      expect(parseCoreRequest({ method, path: '/health' }), String(method)).toBeNull();
    }
  });

  it('refuses a payload that is not an object at all', () => {
    for (const input of [null, undefined, 'GET /health', 7, []]) {
      expect(parseCoreRequest(input)).toBeNull();
    }
  });
});

describe('parseHotkeyMap', () => {
  it('trims an accelerator', () => {
    expect(parseHotkeyMap({ killSwitch: '  Control+Alt+Shift+Q ' })).toEqual({
      killSwitch: 'Control+Alt+Shift+Q',
    });
  });

  it('refuses an empty, oversized or non-string accelerator', () => {
    for (const killSwitch of ['', '   ', 'A'.repeat(65), 42, null]) {
      expect(parseHotkeyMap({ killSwitch })).toBeNull();
    }
    expect(parseHotkeyMap('Control+Q')).toBeNull();
  });
});

describe('bridge handlers', () => {
  describe('core.request', () => {
    it('forwards a valid request and returns the response', async () => {
      const { handlers, svc } = build();
      const result = await handlers.coreRequest({ method: 'POST', path: '/tasks', body: { a: 1 } });
      expect(result).toEqual({ ok: true, value: { status: 200, body: { ok: true } } });
      expect(svc.core.request).toHaveBeenCalledWith({
        method: 'POST',
        path: '/tasks',
        body: { a: 1 },
      });
    });

    it('never reaches the core with a malformed request', async () => {
      const { handlers, svc } = build();
      const result = await handlers.coreRequest({ method: 'GET', path: 'http://evil/' });
      expect(result.ok).toBe(false);
      expect(result.ok ? null : result.error.code).toBe('invalid_request');
      expect(svc.core.request).not.toHaveBeenCalled();
    });

    it('answers `unavailable` while the core is not up', async () => {
      const result = await buildEmpty().coreRequest({ method: 'GET', path: '/health' });
      expect(result.ok).toBe(false);
      expect(result.ok ? null : result.error.code).toBe('unavailable');
    });

    it('turns a thrown gateway error into a result instead of rejecting', async () => {
      const { handlers, svc } = build();
      vi.mocked(svc.core.request).mockRejectedValueOnce(new Error('socket hang up'));
      const result = await handlers.coreRequest({ method: 'GET', path: '/health' });
      expect(result).toEqual({ ok: false, error: { code: 'failed', message: 'socket hang up' } });
    });
  });

  describe('window', () => {
    it('minimises and closes', () => {
      const { handlers, svc } = build();
      handlers.windowMinimize();
      handlers.windowClose();
      expect(svc.window.minimize).toHaveBeenCalledOnce();
      expect(svc.window.close).toHaveBeenCalledOnce();
    });

    it('maximises a restored window and restores a maximised one', () => {
      const { handlers, svc } = build();

      handlers.windowMaximize();
      expect(svc.window.setMaximized).toHaveBeenLastCalledWith(true);

      // The service now reports itself maximised, so the same button restores.
      handlers.windowMaximize();
      expect(svc.window.setMaximized).toHaveBeenLastCalledWith(false);
      expect(svc.window.setMaximized).toHaveBeenCalledTimes(2);
    });

    it('does not throw when there is no window', () => {
      const handlers = buildEmpty();
      expect(() => {
        handlers.windowMinimize();
        handlers.windowMaximize();
        handlers.windowClose();
      }).not.toThrow();
    });

    it('refuses a non-boolean overlay request', async () => {
      const { handlers, svc } = build();
      const result = await handlers.windowSetOverlay('yes');
      expect(result.ok ? null : result.error.code).toBe('invalid_request');
      expect(svc.window.setOverlay).not.toHaveBeenCalled();
    });

    it('reports an overlay that P3-13 has not built as `unavailable`, not `failed`', async () => {
      const svc = services();
      const handlers = createBridgeHandlers({
        core: () => svc.core,
        window: () => ({ ...svc.window, setOverlay: null }),
        hotkeys: () => svc.hotkeys,
        system: () => svc.system,
        updates: () => svc.updates,
        grants: createPathGrants(realpath),
        appInfo: { version: () => '0.0.0', logsPath: () => 'C:\\Logs' },
      });
      const result = await handlers.windowSetOverlay(true);
      expect(result.ok ? null : result.error.code).toBe('unavailable');
    });
  });

  describe('hotkeys', () => {
    it('reads and writes the map', async () => {
      const { handlers } = build();
      expect(await handlers.hotkeysGet()).toEqual({
        ok: true,
        value: { killSwitch: 'Control+Alt+Shift+Q' },
      });
      expect(await handlers.hotkeysSet({ killSwitch: 'Control+Alt+K' })).toEqual({
        ok: true,
        value: { killSwitch: 'Control+Alt+K' },
      });
    });

    it('answers `unavailable` before P3-06 registers anything', async () => {
      const result = await buildEmpty().hotkeysGet();
      expect(result.ok ? null : result.error.code).toBe('unavailable');
    });
  });

  describe('system', () => {
    it('grants the folder the user picked, and only that', async () => {
      const { handlers } = build();
      expect(await handlers.systemPickFolder()).toEqual({ ok: true, value: WORK });
      expect(await handlers.systemOpenPath(`${WORK}\\notes.txt`)).toEqual({
        ok: true,
        value: null,
      });
    });

    it('denies a path before the user has picked anything', async () => {
      const { handlers, svc } = build();
      const result = await handlers.systemOpenPath(`${WORK}\\notes.txt`);
      expect(result.ok ? null : result.error.code).toBe('not_granted');
      expect(svc.system.openPath).not.toHaveBeenCalled();
    });

    it('denies a path outside every granted root', async () => {
      const { handlers, svc } = build();
      await handlers.systemPickFolder();
      const result = await handlers.systemRevealInExplorer('C:\\Users\\Bo\\.ssh\\id_rsa');
      expect(result.ok ? null : result.error.code).toBe('not_granted');
      expect(svc.system.revealInExplorer).not.toHaveBeenCalled();
    });

    it('refuses to launch a program even from a granted folder', async () => {
      const { handlers, svc } = build();
      await handlers.systemPickFolder();
      const result = await handlers.systemOpenPath(`${WORK}\\installer.exe`);
      expect(result.ok ? null : result.error.code).toBe('not_granted');
      expect(svc.system.openPath).not.toHaveBeenCalled();
    });

    it('still reveals that program in Explorer, which shows it rather than running it', async () => {
      const { handlers, svc } = build();
      await handlers.systemPickFolder();
      expect(await handlers.systemRevealInExplorer(`${WORK}\\installer.exe`)).toEqual({
        ok: true,
        value: null,
      });
      expect(svc.system.revealInExplorer).toHaveBeenCalledWith(`${WORK}\\installer.exe`);
    });

    it('hands the shell the resolved path, never the renderer’s spelling', async () => {
      const { handlers, svc } = build();
      await handlers.systemPickFolder();
      await handlers.systemOpenPath(`${WORK}\\sub\\..\\notes.txt`);
      expect(svc.system.openPath).toHaveBeenCalledWith(`${WORK}\\notes.txt`);
    });

    it('refuses a path that is not a string', async () => {
      const { handlers } = build();
      for (const input of [null, 7, {}, '']) {
        const result = await handlers.systemOpenPath(input);
        expect(result.ok ? null : result.error.code).toBe('invalid_request');
      }
    });

    it('reports a cancelled picker as a success with no folder', async () => {
      const { handlers, svc } = build();
      vi.mocked(svc.system.pickFolder).mockResolvedValueOnce(null);
      expect(await handlers.systemPickFolder()).toEqual({ ok: true, value: null });
    });
  });

  describe('updates', () => {
    it('answers `unavailable` before P7-04 wires the updater', async () => {
      const result = await buildEmpty().updatesCheck();
      expect(result.ok ? null : result.error.code).toBe('unavailable');
    });

    it('reports status once the updater exists', async () => {
      const { handlers } = build();
      expect(await handlers.updatesCheck()).toEqual({
        ok: true,
        value: { state: 'idle', version: null, message: null },
      });
    });
  });

  describe('app', () => {
    it('reports the version and the log path', async () => {
      const { handlers } = build();
      expect(await handlers.appVersion()).toBe('0.0.0');
      expect(await handlers.appLogsPath()).toBe('C:\\Logs');
    });
  });

  describe('every handler', () => {
    let empty: BridgeHandlers;

    beforeEach(() => {
      empty = buildEmpty();
    });

    it('resolves rather than rejects, with no service behind it', async () => {
      const calls: Promise<unknown>[] = [
        empty.coreRequest({ method: 'GET', path: '/health' }),
        empty.windowSetOverlay(true),
        empty.hotkeysGet(),
        empty.hotkeysSet({ killSwitch: 'Control+Q' }),
        empty.systemPickFolder(),
        empty.systemOpenPath('C:\\x'),
        empty.systemRevealInExplorer('C:\\x'),
        empty.updatesCheck(),
        empty.updatesInstall(),
        empty.appVersion(),
        empty.appLogsPath(),
      ];
      await expect(Promise.all(calls)).resolves.toBeDefined();
    });
  });
});
