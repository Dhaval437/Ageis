import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { HotkeyMap } from '@aegis/shared';
import {
  DEFAULT_KILL_SWITCH,
  createFileHotkeyStore,
  createHotkeyService,
  displayAccelerator,
  normaliseAccelerator,
  type HotkeyStore,
  type ShortcutRegistry,
} from '../src/main/hotkeys.js';

/** Windows' hotkey table: one owner per combination, and some owned by others. */
class FakeRegistry implements ShortcutRegistry {
  readonly bound = new Map<string, () => void>();
  constructor(readonly takenElsewhere: ReadonlySet<string> = new Set()) {}

  register(accelerator: string, callback: () => void): boolean {
    if (this.takenElsewhere.has(accelerator) || this.bound.has(accelerator)) return false;
    this.bound.set(accelerator, callback);
    return true;
  }

  unregister(accelerator: string): void {
    this.bound.delete(accelerator);
  }

  press(accelerator: string): void {
    this.bound.get(accelerator)?.();
  }
}

function memoryStore(initial: unknown = null): HotkeyStore & { saved: HotkeyMap[] } {
  let current = initial;
  const saved: HotkeyMap[] = [];
  return {
    saved,
    load: () => current,
    save: (map) => {
      saved.push(map);
      current = map;
    },
  };
}

function service(
  registry = new FakeRegistry(),
  store: HotkeyStore = memoryStore(),
  onKillSwitch = vi.fn(),
) {
  const onChange = vi.fn();
  const log = vi.fn();
  const hotkeys = createHotkeyService({ registry, store, onKillSwitch, onChange, log });
  return { hotkeys, registry, onKillSwitch, onChange, log };
}

describe('normaliseAccelerator', () => {
  it('accepts the default and puts any spelling into canonical order', () => {
    expect(normaliseAccelerator(DEFAULT_KILL_SWITCH)).toBe(DEFAULT_KILL_SWITCH);
    expect(normaliseAccelerator('q+shift+CTRL+alt')).toBe('Control+Alt+Shift+Q');
    expect(normaliseAccelerator('Win + Alt + f12')).toBe('Alt+Super+F12');
    expect(normaliseAccelerator('CmdOrCtrl+Shift+esc')).toBe('Control+Shift+Escape');
  });

  it.each([
    ['a single key, which would stop the user typing it', 'Q'],
    ['one modifier, which would steal an app shortcut like Ctrl+Q', 'Control+Q'],
    ['Shift alone, which would steal a capital letter', 'Shift+Q'],
    ['no key at all', 'Control+Alt+Shift'],
    ['two keys', 'Control+Alt+Q+W'],
    ['a repeated modifier', 'Control+Ctrl+Q'],
    ['an empty part', 'Control++Q'],
    ['an unknown key', 'Control+Alt+Pause'],
    ['F25', 'Control+Alt+F25'],
    ['an empty string', ''],
  ])('refuses %s', (_why, raw) => {
    expect(normaliseAccelerator(raw)).toBeNull();
  });
});

describe('displayAccelerator', () => {
  it('uses the names on the keyboard', () => {
    expect(displayAccelerator('Control+Alt+Super+Q')).toBe('Ctrl+Alt+Win+Q');
  });
});

describe('createHotkeyService', () => {
  it('arms the default when nothing is saved, and pressing it fires the kill switch', () => {
    const { hotkeys, registry, onKillSwitch, onChange } = service();
    expect(hotkeys.arm()).toBe(true);
    expect(hotkeys.armed()).toBe(true);
    expect(hotkeys.get()).toEqual({ killSwitch: DEFAULT_KILL_SWITCH });
    expect(onChange).toHaveBeenCalledWith({ killSwitch: DEFAULT_KILL_SWITCH });

    registry.press(DEFAULT_KILL_SWITCH);
    expect(onKillSwitch).toHaveBeenCalledOnce();
  });

  it('arms the saved binding instead of the default', () => {
    const registry = new FakeRegistry();
    const { hotkeys } = service(registry, memoryStore({ killSwitch: 'ctrl+shift+f12' }));
    hotkeys.arm();
    expect(hotkeys.get()).toEqual({ killSwitch: 'Control+Shift+F12' });
    expect([...registry.bound.keys()]).toEqual(['Control+Shift+F12']);
  });

  it.each([
    ['an invalid saved binding', { killSwitch: 'Q' }],
    ['a saved value that is not a map', 'Control+Alt+Q'],
    ['a saved map with no binding', {}],
  ])('falls back to the default for %s', (_why, stored) => {
    const { hotkeys } = service(new FakeRegistry(), memoryStore(stored));
    expect(hotkeys.arm()).toBe(true);
    expect(hotkeys.get()).toEqual({ killSwitch: DEFAULT_KILL_SWITCH });
  });

  it('falls back to the default when another program owns the saved binding', () => {
    const registry = new FakeRegistry(new Set(['Control+Shift+F12']));
    const { hotkeys, log } = service(registry, memoryStore({ killSwitch: 'Control+Shift+F12' }));
    expect(hotkeys.arm()).toBe(true);
    expect(hotkeys.get()).toEqual({ killSwitch: DEFAULT_KILL_SWITCH });
    expect(log).toHaveBeenCalledWith(expect.stringContaining('Control+Shift+F12'));
  });

  it('says so, rather than showing a binding that does nothing, when it cannot arm', () => {
    const registry = new FakeRegistry(new Set([DEFAULT_KILL_SWITCH]));
    const { hotkeys, log } = service(registry);
    expect(hotkeys.arm()).toBe(false);
    expect(hotkeys.armed()).toBe(false);
    expect(() => hotkeys.get()).toThrow(/not armed.*Ctrl\+Alt\+Shift\+Q/);
    expect(log).toHaveBeenCalledWith(expect.stringContaining('NOT armed'));
  });

  it('rebinds: the new shortcut fires, the old one is released, the choice is saved', async () => {
    const store = memoryStore();
    const { hotkeys, registry, onKillSwitch, onChange } = service(new FakeRegistry(), store);
    hotkeys.arm();

    await expect(hotkeys.set({ killSwitch: 'alt+shift+k' })).resolves.toEqual({
      killSwitch: 'Alt+Shift+K',
    });
    expect([...registry.bound.keys()]).toEqual(['Alt+Shift+K']);
    expect(store.saved).toEqual([{ killSwitch: 'Alt+Shift+K' }]);
    expect(onChange).toHaveBeenLastCalledWith({ killSwitch: 'Alt+Shift+K' });

    registry.press(DEFAULT_KILL_SWITCH);
    expect(onKillSwitch).not.toHaveBeenCalled();
    registry.press('Alt+Shift+K');
    expect(onKillSwitch).toHaveBeenCalledOnce();
  });

  it('refuses an invalid shortcut and keeps the old one armed', async () => {
    const { hotkeys, registry } = service();
    hotkeys.arm();
    await expect(hotkeys.set({ killSwitch: 'Control+Q' })).rejects.toThrow(/two or more/);
    expect([...registry.bound.keys()]).toEqual([DEFAULT_KILL_SWITCH]);
  });

  it('refuses a shortcut another program owns, and the old one never goes unbound', async () => {
    const registry = new FakeRegistry(new Set(['Control+Alt+Delete']));
    const { hotkeys } = service(registry);
    hotkeys.arm();
    await expect(hotkeys.set({ killSwitch: 'Control+Alt+Delete' })).rejects.toThrow(
      'Another program is already using Ctrl+Alt+Delete. Choose a different shortcut.',
    );
    expect([...registry.bound.keys()]).toEqual([DEFAULT_KILL_SWITCH]);
    expect(hotkeys.get()).toEqual({ killSwitch: DEFAULT_KILL_SWITCH });
  });

  it('rolls back when the choice cannot be saved', async () => {
    const store: HotkeyStore = {
      load: () => null,
      save: () => {
        throw new Error('EACCES: C:\\Users\\someone\\AppData\\Local\\Aegis\\hotkeys.json');
      },
    };
    const { hotkeys, registry } = service(new FakeRegistry(), store);
    hotkeys.arm();
    await expect(hotkeys.set({ killSwitch: 'Alt+Shift+K' })).rejects.toThrow(
      'Aegis could not save the shortcut. Nothing was changed.',
    );
    expect([...registry.bound.keys()]).toEqual([DEFAULT_KILL_SWITCH]);
  });

  it('setting the binding already in force changes nothing', async () => {
    const store = memoryStore();
    const { hotkeys } = service(new FakeRegistry(), store);
    hotkeys.arm();
    await hotkeys.set({ killSwitch: 'ctrl+alt+shift+q' });
    expect(store.saved).toEqual([]);
  });

  it('arms the kill switch by rebinding when it could not arm at start', async () => {
    const registry = new FakeRegistry(new Set([DEFAULT_KILL_SWITCH]));
    const { hotkeys } = service(registry);
    hotkeys.arm();
    await hotkeys.set({ killSwitch: 'Alt+Shift+K' });
    expect(hotkeys.armed()).toBe(true);
    expect(hotkeys.get()).toEqual({ killSwitch: 'Alt+Shift+K' });
  });

  it('a registry that throws on a binding fails closed', () => {
    const registry: ShortcutRegistry = {
      register: () => {
        throw new TypeError('conversion failure');
      },
      unregister: vi.fn(),
    };
    const { hotkeys } = service(registry as FakeRegistry);
    expect(hotkeys.arm()).toBe(false);
  });

  it('dispose releases the binding', () => {
    const { hotkeys, registry } = service();
    hotkeys.arm();
    hotkeys.dispose();
    expect(registry.bound.size).toBe(0);
    expect(hotkeys.armed()).toBe(false);
  });
});

describe('createFileHotkeyStore', () => {
  let dir: string | null = null;
  afterEach(() => {
    if (dir !== null) rmSync(dir, { recursive: true, force: true });
    dir = null;
  });

  it('round-trips a binding, creating the folder, and leaves no temporary file', () => {
    dir = mkdtempSync(join(tmpdir(), 'aegis-hotkeys-'));
    const path = join(dir, 'Aegis', 'hotkeys.json');
    const store = createFileHotkeyStore(path);
    expect(store.load()).toBeNull();

    store.save({ killSwitch: 'Alt+Shift+K' });
    expect(store.load()).toEqual({ killSwitch: 'Alt+Shift+K' });
    expect(() => readFileSync(`${path}.tmp`)).toThrow();
  });

  it('reads a corrupt file as nothing saved', () => {
    dir = mkdtempSync(join(tmpdir(), 'aegis-hotkeys-'));
    const path = join(dir, 'hotkeys.json');
    writeFileSync(path, '{"killSwitch": ');
    expect(createFileHotkeyStore(path).load()).toBeNull();
  });
});
