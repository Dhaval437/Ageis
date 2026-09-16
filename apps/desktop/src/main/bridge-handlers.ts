/**
 * What each bridge channel actually does.
 *
 * Deliberately `electron`-free — every capability arrives as an injected
 * service — for two reasons. It makes the whole surface unit-testable without
 * an Electron process, and it lets the channels exist now while the subsystems
 * behind them land later: a service that is `null` answers `unavailable`
 * instead of the channel not existing, so the renderer sees one honest error
 * shape from day one. `ipc.ts` supplies the real services.
 *
 * Two rules hold for every handler here:
 *  1. **Arguments are untrusted.** The renderer renders text the agent scraped
 *     off the user's screen. Every payload is validated here, in MAIN, before
 *     it reaches a service.
 *  2. **Handlers resolve, they never reject.** See `BridgeResult` in
 *     `@aegis/shared`.
 */

import type {
  BridgeError,
  BridgeResult,
  CoreMethod,
  CoreRequest,
  CoreResponse,
  HotkeyMap,
  UpdateStatus,
} from '@aegis/shared';
import type { PathGrants } from './path-grants.js';

/**
 * The core's REST API, reachable once the supervisor has it up (P0-06).
 * Implementations own the bearer token, the `127.0.0.1` origin and the request
 * timeout — none of which the renderer may name.
 */
export interface CoreGateway {
  readonly request: (request: CoreRequest) => Promise<CoreResponse>;
}

/** Global shortcuts (P3-06). */
export interface HotkeyService {
  readonly get: () => HotkeyMap;
  /** Resolves with the map in force, which may differ if a binding was refused. */
  readonly set: (hotkeys: HotkeyMap) => Promise<HotkeyMap>;
}

/** Auto-update (P7-04). */
export interface UpdateService {
  readonly check: () => Promise<UpdateStatus>;
  readonly install: () => Promise<void>;
}

export interface WindowService {
  readonly minimize: () => void;
  /**
   * Read and written separately, rather than as one `maximize()` toggle, so the
   * decision "maximised → restore" lives in this electron-free file and has a
   * test. `ipc.ts` only supplies the two Electron calls.
   */
  readonly isMaximized: () => boolean;
  readonly setMaximized: (maximized: boolean) => void;
  readonly close: () => void;
  /**
   * Shows or hides the `OverlayHUD` window, or `null` until P3-13 builds it —
   * so "not built yet" reports `unavailable` rather than `failed`. The two are
   * different things and the renderer has to be able to tell them apart.
   */
  readonly setOverlay: ((visible: boolean) => Promise<void>) | null;
}

export interface SystemService {
  /** The OS folder picker. Resolves `null` if the user cancelled. */
  readonly pickFolder: () => Promise<string | null>;
  /** Opens a resolved, granted path with its default handler. */
  readonly openPath: (path: string) => Promise<void>;
  /** Reveals a resolved, granted path in Explorer. */
  readonly revealInExplorer: (path: string) => void;
}

export interface AppInfo {
  readonly version: () => string;
  readonly logsPath: () => string;
}

/**
 * Services are late-bound getters, not values: the window is replaced when it
 * is reopened, and the core is respawned on a 3-strike rule (P0-07). A handler
 * that captured either at registration time would hold a dead reference.
 */
export interface BridgeDependencies {
  readonly core: () => CoreGateway | null;
  readonly window: () => WindowService | null;
  readonly hotkeys: () => HotkeyService | null;
  readonly system: () => SystemService | null;
  readonly updates: () => UpdateService | null;
  readonly grants: PathGrants;
  readonly appInfo: AppInfo;
}

export interface BridgeHandlers {
  readonly coreRequest: (input: unknown) => Promise<BridgeResult<CoreResponse>>;
  readonly windowMinimize: () => void;
  /** Maximises the window, or restores it if it already is (P0-15). */
  readonly windowMaximize: () => void;
  readonly windowClose: () => void;
  readonly windowSetOverlay: (input: unknown) => Promise<BridgeResult<null>>;
  readonly hotkeysGet: () => Promise<BridgeResult<HotkeyMap>>;
  readonly hotkeysSet: (input: unknown) => Promise<BridgeResult<HotkeyMap>>;
  readonly systemPickFolder: () => Promise<BridgeResult<string | null>>;
  readonly systemOpenPath: (input: unknown) => Promise<BridgeResult<null>>;
  readonly systemRevealInExplorer: (input: unknown) => Promise<BridgeResult<null>>;
  readonly updatesCheck: () => Promise<BridgeResult<UpdateStatus>>;
  readonly updatesInstall: () => Promise<BridgeResult<null>>;
  readonly appVersion: () => Promise<string>;
  readonly appLogsPath: () => Promise<string>;
}

const CORE_METHODS: readonly string[] = ['GET', 'POST', 'PUT', 'DELETE'];

/** Longer than any route in `ARCHITECTURE.md § 9.1` and short enough to bound the work. */
const MAX_PATH_LENGTH = 2048;

function ok<T>(value: T): BridgeResult<T> {
  return { ok: true, value };
}

function fail<T>(code: BridgeError['code'], message: string): BridgeResult<T> {
  return { ok: false, error: { code, message } };
}

const UNAVAILABLE = 'That part of Aegis is not running yet.';

/**
 * Turns a thrown error into a result. The message is kept because it is shown
 * to the developer in the timeline, but nothing structured is forwarded — a
 * rejected value from a service may hold a filesystem path or a response body.
 */
function failedFrom(error: unknown): BridgeError {
  return {
    code: 'failed',
    message: error instanceof Error ? error.message : 'The operation failed.',
  };
}

/**
 * The path the renderer may name is a *path*, not a URL. Anything that could
 * re-target the request — a scheme, an authority, a backslash Windows would
 * fold to a separator, a `..` segment — is refused rather than normalised,
 * because normalising is where these bugs live.
 */
export function parseCoreRequest(input: unknown): CoreRequest | null {
  if (typeof input !== 'object' || input === null) return null;
  const candidate = input as Record<string, unknown>;

  const { method, path } = candidate;
  if (typeof method !== 'string' || !CORE_METHODS.includes(method)) return null;
  if (typeof path !== 'string') return null;
  if (path.length === 0 || path.length > MAX_PATH_LENGTH) return null;
  if (!path.startsWith('/')) return null;
  // `//host/x` is protocol-relative; `/\host` is the same trick with the slash
  // Windows also accepts.
  if (path.startsWith('//') || path.startsWith('/\\')) return null;
  if (path.includes('\\')) return null;
  if (path.split(/[?#]/, 1)[0]?.split('/').includes('..') === true) return null;
  // Control characters split requests apart in a header-ish way. Never.
  // eslint-disable-next-line no-control-regex -- matching them is the whole point
  if (/[\u0000-\u001f\u007f]/.test(path)) return null;

  return { method: method as CoreMethod, path, body: candidate['body'] };
}

export function parseHotkeyMap(input: unknown): HotkeyMap | null {
  if (typeof input !== 'object' || input === null) return null;
  const { killSwitch } = input as Record<string, unknown>;
  if (typeof killSwitch !== 'string') return null;
  const trimmed = killSwitch.trim();
  if (trimmed === '' || trimmed.length > 64) return null;
  return { killSwitch: trimmed };
}

export function createBridgeHandlers(deps: BridgeDependencies): BridgeHandlers {
  /** Shared shape for the "resolve a path the renderer named" pair. */
  async function withGrantedPath(
    input: unknown,
    mode: 'check' | 'checkOpenable',
    run: (system: SystemService, path: string) => void | Promise<void>,
  ): Promise<BridgeResult<null>> {
    if (typeof input !== 'string' || input === '' || input.length > MAX_PATH_LENGTH) {
      return fail('invalid_request', 'A path is required.');
    }
    const system = deps.system();
    if (system === null) return fail('unavailable', UNAVAILABLE);

    const result = await deps.grants[mode](input);
    if (!result.ok) {
      return result.reason === 'executable'
        ? fail('not_granted', 'Aegis will not open a program for you. Open it yourself.')
        : fail('not_granted', 'That path is outside the folders you have chosen.');
    }

    try {
      await run(system, result.path);
      return ok(null);
    } catch (error: unknown) {
      return { ok: false, error: failedFrom(error) };
    }
  }

  return {
    coreRequest: async (input: unknown): Promise<BridgeResult<CoreResponse>> => {
      const request = parseCoreRequest(input);
      if (request === null)
        return fail('invalid_request', 'That request is not a valid core call.');

      const core = deps.core();
      if (core === null) return fail('unavailable', 'Aegis is not connected to its core yet.');

      try {
        return ok(await core.request(request));
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    windowMinimize: (): void => {
      deps.window()?.minimize();
    },

    windowMaximize: (): void => {
      const window = deps.window();
      if (window === null) return;
      // The button is one button: whichever state the window is in, this leaves
      // it in the other one. The renderer never says which, so it cannot get the
      // two out of step with the window MAIN actually owns.
      window.setMaximized(!window.isMaximized());
    },

    windowClose: (): void => {
      deps.window()?.close();
    },

    windowSetOverlay: async (input: unknown): Promise<BridgeResult<null>> => {
      if (typeof input !== 'boolean') return fail('invalid_request', 'Expected true or false.');
      const window = deps.window();
      if (window === null || window.setOverlay === null) return fail('unavailable', UNAVAILABLE);
      try {
        await window.setOverlay(input);
        return ok(null);
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    hotkeysGet: (): Promise<BridgeResult<HotkeyMap>> => {
      const hotkeys = deps.hotkeys();
      if (hotkeys === null) return Promise.resolve(fail('unavailable', UNAVAILABLE));
      try {
        return Promise.resolve(ok(hotkeys.get()));
      } catch (error: unknown) {
        return Promise.resolve({ ok: false, error: failedFrom(error) });
      }
    },

    hotkeysSet: async (input: unknown): Promise<BridgeResult<HotkeyMap>> => {
      const requested = parseHotkeyMap(input);
      if (requested === null) return fail('invalid_request', 'That is not a valid shortcut.');
      const hotkeys = deps.hotkeys();
      if (hotkeys === null) return fail('unavailable', UNAVAILABLE);
      try {
        return ok(await hotkeys.set(requested));
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    systemPickFolder: async (): Promise<BridgeResult<string | null>> => {
      const system = deps.system();
      if (system === null) return fail('unavailable', UNAVAILABLE);
      try {
        const picked = await system.pickFolder();
        if (picked === null) return ok(null);
        // The user chose it, so it becomes reachable by `openPath` — and only
        // now. This is the single way a root is granted.
        await deps.grants.grantRoot(picked);
        return ok(picked);
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    systemOpenPath: async (input: unknown): Promise<BridgeResult<null>> =>
      withGrantedPath(input, 'checkOpenable', async (system, path) => {
        await system.openPath(path);
      }),

    systemRevealInExplorer: async (input: unknown): Promise<BridgeResult<null>> =>
      withGrantedPath(input, 'check', (system, path) => {
        system.revealInExplorer(path);
      }),

    updatesCheck: async (): Promise<BridgeResult<UpdateStatus>> => {
      const updates = deps.updates();
      if (updates === null) return fail('unavailable', UNAVAILABLE);
      try {
        return ok(await updates.check());
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    updatesInstall: async (): Promise<BridgeResult<null>> => {
      const updates = deps.updates();
      if (updates === null) return fail('unavailable', UNAVAILABLE);
      try {
        await updates.install();
        return ok(null);
      } catch (error: unknown) {
        return { ok: false, error: failedFrom(error) };
      }
    },

    appVersion: (): Promise<string> => Promise.resolve(deps.appInfo.version()),

    appLogsPath: (): Promise<string> => Promise.resolve(deps.appInfo.logsPath()),
  };
}
