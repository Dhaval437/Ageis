import type { ScopeInfo } from '@aegis/shared';

/**
 * Runtime checks for what the scope routes answer (`P3-17`). The folders are the
 * person's own paths, shown as text; nothing here trusts their shape beyond "a string".
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strings(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const items: unknown[] = value;
  return items.every((item): item is string => typeof item === 'string') ? items : null;
}

export function parseScope(value: unknown): ScopeInfo | null {
  if (!isRecord(value)) return null;
  const { id, name } = value;
  if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1) return null;
  if (typeof name !== 'string') return null;
  const folders = strings(value['folders']);
  const apps = strings(value['apps']);
  if (folders === null || apps === null) return null;
  return { id, name, folders, apps };
}

export function parseScopeList(value: unknown): ScopeInfo[] | null {
  if (!isRecord(value) || !Array.isArray(value['scopes'])) return null;
  const scopes = (value['scopes'] as unknown[]).map(parseScope);
  return scopes.every((scope): scope is ScopeInfo => scope !== null) ? scopes : null;
}

/**
 * `UI.md § 8.3`'s "what this allows", in plain English (`§ 11`). It says what the
 * Guardian actually does (`ARCHITECTURE.md § 8.2`): inside the folders the agent may
 * read and change files; outside them it must ask to read and may not change anything;
 * and the places Aegis never touches stay closed even inside.
 */
export function whatThisAllows(scope: Pick<ScopeInfo, 'folders'>): string {
  const count = scope.folders.length;
  if (count === 0) {
    return 'Aegis can reach no folders with this scope. It must ask before reading anything, and it cannot change any file.';
  }
  const these = count === 1 ? 'this folder' : `these ${String(count)} folders`;
  return `Aegis may read and change files in ${these}. Anywhere else it must ask before reading, and it cannot change anything. Places Aegis never touches stay off-limits even here.`;
}
