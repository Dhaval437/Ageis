/**
 * Where Aegis keeps its files on disk.
 *
 * There is exactly one Aegis log folder and the **core** decides where it is:
 * `aegis_core/logging_setup.py` writes to `%LOCALAPPDATA%\Aegis\logs`. Electron's
 * own `app.getPath('logs')` is somewhere else entirely (`%APPDATA%\Aegis\logs`),
 * and nothing writes there, so "Open logs" pointed at it would open an empty
 * folder while the engine's log sat next door. This mirrors the core's rule
 * instead, in one electron-free place both `ipc.ts` and `diagnostics.ts` can use.
 */

import { join } from 'node:path';

/** The same fallback the core uses when `LOCALAPPDATA` is not set (non-Windows dev). */
function dataRoot(env: NodeJS.ProcessEnv, homeDir: string): string {
  const localAppData = env['LOCALAPPDATA'];
  if (localAppData !== undefined && localAppData.length > 0) return join(localAppData, 'Aegis');
  return join(homeDir, '.aegis');
}

/** `%LOCALAPPDATA%\Aegis\logs` — the folder `core.log` and its rotations live in. */
export function aegisLogDir(env: NodeJS.ProcessEnv, homeDir: string): string {
  return join(dataRoot(env, homeDir), 'logs');
}

/**
 * `%LOCALAPPDATA%\Aegis\hotkeys.json` — the kill-switch binding (`P3-06`). MAIN's
 * own file, not a core setting: the shortcut has to arm when the core is hung or
 * has never started.
 */
export function hotkeysFile(env: NodeJS.ProcessEnv, homeDir: string): string {
  return join(dataRoot(env, homeDir), 'hotkeys.json');
}
