/**
 * Global shortcuts, including the kill switch.
 *
 * The kill switch lives here, in MAIN, precisely so a hung Python core cannot
 * disable it (REMEMBER.md invariant 2). What pressing it *does* is
 * `kill-switch.ts`; this module only owns the binding — which accelerator, is it
 * actually registered with Windows, and what the user rebinds it to.
 *
 * `electron`-free at runtime, like `supervisor.ts`: Electron's `globalShortcut`
 * satisfies `ShortcutRegistry` and is injected by `index.ts`, so every rule here
 * is testable without an Electron process.
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { HotkeyMap } from '@aegis/shared';
import type { HotkeyService } from './bridge-handlers.js';

/** `ARCHITECTURE.md § 8.3`. Also what the tray shows until the user rebinds it. */
export const DEFAULT_KILL_SWITCH = 'Control+Alt+Shift+Q';

/** Canonical order, and every spelling Electron accepts for each. */
const MODIFIERS: readonly (readonly [string, readonly string[]])[] = [
  ['Control', ['control', 'ctrl', 'commandorcontrol', 'cmdorctrl']],
  ['Alt', ['alt', 'option']],
  ['Shift', ['shift']],
  ['Super', ['super', 'meta', 'win', 'windows']],
];

const NAMED_KEYS: readonly string[] = [
  'Space',
  'Tab',
  'Backspace',
  'Delete',
  'Insert',
  'Home',
  'End',
  'PageUp',
  'PageDown',
  'Up',
  'Down',
  'Left',
  'Right',
  'Escape',
];

/** One letter, one digit, or F1–F24, in the spelling Electron expects. */
function canonicalKey(token: string): string | null {
  if (/^[a-z0-9]$/i.test(token)) return token.toUpperCase();
  const fKey = /^f([1-9]|1[0-9]|2[0-4])$/i.exec(token);
  if (fKey !== null) return `F${fKey[1] ?? ''}`;
  if (token.toLowerCase() === 'esc') return 'Escape';
  return NAMED_KEYS.find((name) => name.toLowerCase() === token.toLowerCase()) ?? null;
}

/**
 * The accelerator in canonical form, or `null` if it cannot be a kill switch.
 *
 * **Two or more modifiers plus exactly one key.** Windows' `RegisterHotKey`
 * takes a shortcut away from every other program on the machine, so `Q` alone
 * would stop the user typing the letter and `Ctrl+Q` would break *Quit* in half
 * their apps — a kill switch that costs that much gets rebound to nothing.
 */
export function normaliseAccelerator(raw: string): string | null {
  const tokens = raw.split('+').map((token) => token.trim());
  if (tokens.some((token) => token === '')) return null;

  const modifiers = new Set<string>();
  const keys: string[] = [];
  for (const token of tokens) {
    const lower = token.toLowerCase();
    const modifier = MODIFIERS.find(([, spellings]) => spellings.includes(lower));
    if (modifier !== undefined) {
      if (modifiers.has(modifier[0])) return null;
      modifiers.add(modifier[0]);
      continue;
    }
    const key = canonicalKey(token);
    if (key === null) return null;
    keys.push(key);
  }
  if (keys.length !== 1 || modifiers.size < 2) return null;

  const ordered = MODIFIERS.map(([name]) => name).filter((name) => modifiers.has(name));
  return [...ordered, keys[0]].join('+');
}

/** How a shortcut reads to a person: `Ctrl+Alt+Shift+Q`, `Win` for `Super`. */
export function displayAccelerator(accelerator: string): string {
  return accelerator.replace('Control', 'Ctrl').replace('Super', 'Win');
}

/** Electron's `globalShortcut`, as much of it as this module uses. */
export interface ShortcutRegistry {
  register(accelerator: string, callback: () => void): boolean;
  unregister(accelerator: string): void;
}

/** Where the user's binding survives a restart. */
export interface HotkeyStore {
  /** The saved map, or `null` if there is none or it cannot be read. Never throws. */
  load(): unknown;
  /** Throws if the map could not be written. */
  save(map: HotkeyMap): void;
}

/**
 * A JSON file in Aegis' own data folder, replaced atomically — a torn write
 * must never leave the kill switch unbindable after a crash.
 */
export function createFileHotkeyStore(path: string): HotkeyStore {
  return {
    load: (): unknown => {
      try {
        return JSON.parse(readFileSync(path, 'utf8')) as unknown;
      } catch {
        // Missing on first run; unreadable if hand-edited. Either way the
        // default applies, which is the one binding the user was taught.
        return null;
      }
    },
    save: (map: HotkeyMap): void => {
      mkdirSync(dirname(path), { recursive: true });
      const temporary = `${path}.tmp`;
      writeFileSync(temporary, `${JSON.stringify({ killSwitch: map.killSwitch })}\n`, 'utf8');
      renameSync(temporary, path);
    },
  };
}

export interface HotkeyServiceOptions {
  readonly registry: ShortcutRegistry;
  readonly store: HotkeyStore;
  /** What the kill switch does. Runs on MAIN's event loop, never in the core. */
  readonly onKillSwitch: () => void;
  /** Told whenever the armed binding changes, so the tray can show it. */
  readonly onChange?: (map: HotkeyMap) => void;
  readonly log?: (message: string) => void;
}

export interface KillSwitchHotkeys extends HotkeyService {
  /**
   * Register the saved binding, falling back to the default. Returns whether
   * the kill switch is armed; `false` means another program owns both.
   */
  readonly arm: () => boolean;
  readonly armed: () => boolean;
  /** The binding in force, or the one we tried to arm. */
  readonly current: () => HotkeyMap;
  readonly dispose: () => void;
}

export function createHotkeyService(options: HotkeyServiceOptions): KillSwitchHotkeys {
  const { registry, store } = options;
  const log = options.log ?? ((message: string): void => console.warn(message));
  let binding = DEFAULT_KILL_SWITCH;
  let isArmed = false;

  function register(accelerator: string): boolean {
    try {
      return registry.register(accelerator, options.onKillSwitch);
    } catch {
      // Electron throws on an accelerator it cannot parse. `normaliseAccelerator`
      // should make that impossible, and a false here fails closed either way.
      return false;
    }
  }

  function saved(): string | null {
    const loaded = store.load();
    if (typeof loaded !== 'object' || loaded === null) return null;
    const { killSwitch } = loaded as Record<string, unknown>;
    return typeof killSwitch === 'string' ? normaliseAccelerator(killSwitch) : null;
  }

  function arm(): boolean {
    if (isArmed) return true;
    const candidates = [saved(), DEFAULT_KILL_SWITCH].filter(
      (candidate, index, all): candidate is string =>
        candidate !== null && all.indexOf(candidate) === index,
    );
    for (const candidate of candidates) {
      binding = candidate;
      if (register(candidate)) {
        isArmed = true;
        options.onChange?.({ killSwitch: binding });
        return true;
      }
      log(`[hotkeys] ${candidate} is taken by another program`);
    }
    // Invariant 2 with the switch unarmed. The tray item still works, and
    // `get()` refuses so the Settings screen says so instead of showing a
    // binding that does nothing.
    log('[hotkeys] the kill switch is NOT armed');
    return false;
  }

  function set(requested: HotkeyMap): Promise<HotkeyMap> {
    const next = normaliseAccelerator(requested.killSwitch);
    if (next === null) {
      return Promise.reject(
        new Error('Use two or more of Ctrl, Alt, Shift and Win, plus one letter, digit or F-key.'),
      );
    }
    if (isArmed && next === binding) return Promise.resolve({ killSwitch: binding });

    // The new one first: the kill switch is never unbound in between, and a
    // shortcut Windows refuses leaves the old one exactly where it was.
    if (!register(next)) {
      return Promise.reject(
        new Error(
          `Another program is already using ${displayAccelerator(next)}. Choose a different shortcut.`,
        ),
      );
    }
    try {
      store.save({ killSwitch: next });
    } catch {
      registry.unregister(next);
      return Promise.reject(new Error('Aegis could not save the shortcut. Nothing was changed.'));
    }
    if (isArmed) registry.unregister(binding);
    binding = next;
    isArmed = true;
    options.onChange?.({ killSwitch: binding });
    return Promise.resolve({ killSwitch: binding });
  }

  return {
    arm,
    armed: (): boolean => isArmed,
    current: (): HotkeyMap => ({ killSwitch: binding }),
    get: (): HotkeyMap => {
      if (!isArmed) {
        throw new Error(
          `The kill switch is not armed: another program is using ${displayAccelerator(binding)}. Choose a different shortcut.`,
        );
      }
      return { killSwitch: binding };
    },
    set,
    dispose: (): void => {
      if (isArmed) registry.unregister(binding);
      isArmed = false;
    },
  };
}
