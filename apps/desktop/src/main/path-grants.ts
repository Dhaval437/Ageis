/**
 * Which paths the renderer is allowed to hand to the OS shell.
 *
 * `system.openPath` launches a file with its default handler and
 * `system.revealInExplorer` opens a folder around it. Both take a string from
 * the renderer — and the renderer displays text the agent scraped off the
 * user's screen, which `REMEMBER.md § 8.2` names as the least-defended attack
 * class in this product. An unrestricted `openPath` would turn "a webpage said
 * so" into "Aegis launched it".
 *
 * So a path is only usable if it resolves inside a root the **user** granted:
 * a folder in one of their scopes — each one picked in the OS dialog (P3-17) —
 * or one of Aegis' own data directories. The scopes are the one model of "the
 * folders the person chose"; this module asks for them at every check rather
 * than keeping a second list that could disagree. The
 * check runs on the resolved real path, immediately before the call
 * (`REVIEW.md § 5`), because `..`, a symlink, a junction or an 8.3 short name
 * all let a string point somewhere its spelling does not.
 *
 * `electron`-free so it is testable without an Electron process; the real
 * `realpath` is injected by `ipc.ts`.
 */

import { resolve } from 'node:path';

/** Resolves symlinks/junctions/8.3 names. Rejects if the path does not exist. */
export type Realpath = (path: string) => Promise<string>;

/**
 * Extensions that execute when opened. Denied even inside a granted root: the
 * user granted a folder for their documents, not a launcher for whatever
 * lands in it.
 */
const EXECUTABLE_EXTENSIONS: readonly string[] = [
  '.bat',
  '.chm',
  '.cmd',
  '.com',
  '.cpl',
  '.exe',
  '.hta',
  '.js',
  '.jse',
  '.lnk',
  '.msc',
  '.msi',
  '.msp',
  '.pif',
  '.ps1',
  '.reg',
  '.scr',
  '.url',
  '.vb',
  '.vbe',
  '.vbs',
  '.wsf',
  '.wsh',
];

export type GrantDenial = 'not_granted' | 'executable';

export type GrantCheck =
  | { readonly ok: true; readonly path: string }
  | { readonly ok: false; readonly reason: GrantDenial };

/** The folders of every stored scope, or none if they cannot be had right now. */
export type ScopeRoots = () => Promise<readonly string[]>;

export interface PathGrants {
  /** Records one of Aegis' own roots. Silently ignores a path that cannot be resolved. */
  readonly grantRoot: (path: string) => Promise<void>;
  /** Resolves a path and confirms it sits inside a granted root. */
  readonly check: (path: string) => Promise<GrantCheck>;
  /** As `check`, and additionally refuses anything that executes when opened. */
  readonly checkOpenable: (path: string) => Promise<GrantCheck>;
  /** Aegis' own granted roots, resolved (not the scopes). For diagnostics and tests. */
  readonly roots: () => readonly string[];
}

export function createPathGrants(realpath: Realpath, scopeRoots?: ScopeRoots): PathGrants {
  const granted: string[] = [];

  async function resolveReal(path: string): Promise<string | null> {
    if (typeof path !== 'string' || path === '') return null;
    try {
      return await realpath(resolve(path));
    } catch {
      // A path that does not exist cannot be granted and cannot be opened.
      return null;
    }
  }

  async function currentScopeRoots(): Promise<readonly string[]> {
    if (scopeRoots === undefined) return [];
    try {
      return (await scopeRoots()).filter((root) => typeof root === 'string' && root !== '');
    } catch {
      // No core, or it could not answer: only Aegis' own folders are open.
      return [];
    }
  }

  async function check(path: string): Promise<GrantCheck> {
    const real = await resolveReal(path);
    if (real === null) return { ok: false, reason: 'not_granted' };
    const roots = [...granted, ...(await currentScopeRoots())];
    if (!roots.some((root) => isWithin(root, real))) {
      return { ok: false, reason: 'not_granted' };
    }
    return { ok: true, path: real };
  }

  return {
    grantRoot: async (path: string): Promise<void> => {
      const real = await resolveReal(path);
      if (real === null) return;
      if (!granted.includes(real)) granted.push(real);
    },

    check,

    checkOpenable: async (path: string): Promise<GrantCheck> => {
      const result = await check(path);
      if (!result.ok) return result;
      if (isExecutable(result.path)) return { ok: false, reason: 'executable' };
      return result;
    },

    roots: (): readonly string[] => [...granted],
  };
}

/**
 * Containment on a path-separator boundary, so `C:\Users\Bo` never contains
 * `C:\Users\Bob`. Case-insensitive because Windows filesystems are.
 */
export function isWithin(root: string, candidate: string): boolean {
  const normalisedRoot = root.replace(/[\\/]+$/, '').toLowerCase();
  const normalisedCandidate = candidate.replace(/[\\/]+$/, '').toLowerCase();
  if (normalisedRoot === '') return false;
  if (normalisedCandidate === normalisedRoot) return true;
  return (
    normalisedCandidate.startsWith(`${normalisedRoot}\\`) ||
    normalisedCandidate.startsWith(`${normalisedRoot}/`)
  );
}

export function isExecutable(path: string): boolean {
  // Trailing dots and spaces are stripped by Win32 before the file is opened,
  // so `payload.exe.` and `payload.exe ` both run `payload.exe`.
  const trimmed = path.replace(/[.\s]+$/, '').toLowerCase();
  return EXECUTABLE_EXTENSIONS.some((extension) => trimmed.endsWith(extension));
}

/**
 * Every scope folder, from `GET /v1/scopes` through MAIN's own gateway. The core has
 * already re-validated and canonicalised them; anything that is not a list of strings
 * counts as no folders at all.
 */
export async function scopeFoldersFrom(
  request:
    | ((request: { method: 'GET'; path: string }) => Promise<{ status: number; body: unknown }>)
    | null,
): Promise<readonly string[]> {
  if (request === null) return [];
  const response = await request({ method: 'GET', path: '/scopes' });
  if (response.status !== 200 || typeof response.body !== 'object' || response.body === null) {
    return [];
  }
  const { scopes } = response.body as { scopes?: unknown };
  if (!Array.isArray(scopes)) return [];
  const folders: string[] = [];
  for (const scope of scopes as unknown[]) {
    if (typeof scope !== 'object' || scope === null) continue;
    const listed = (scope as { folders?: unknown }).folders;
    if (!Array.isArray(listed)) continue;
    for (const folder of listed as unknown[]) {
      if (typeof folder === 'string' && folder !== '') folders.push(folder);
    }
  }
  return folders;
}
