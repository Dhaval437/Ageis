import type {
  AegisBridge,
  BridgeResult,
  CoreRequest,
  CoreResponse,
  ScopeInfo,
} from '@aegis/shared';
import { coreDetail } from '@/lib/models-api';
import { parseScope, parseScopeList } from '@/lib/scopes-api';
import { useScopesStore, type ScopesState } from '@/stores/scopes';

/**
 * Everything the scope screens ask for (`P3-17`), and the only writer of the scopes store.
 *
 * **A folder enters a scope only through `window.aegis.scopes`**, which opens the OS
 * dialog in MAIN and sends the pick to the core signed as MAIN's. Nothing here ever
 * names a folder to add. What only narrows — reading, renaming, dropping a folder,
 * deleting — goes through `core.request`, and the core refuses a "narrowing" that
 * would add anything.
 *
 * Copy follows `UI.md § 11`: a 4xx `detail` is the core's sentence for the person
 * (`ScopeError`); anything else is said in our words.
 */

type Bridge = Pick<AegisBridge, 'core' | 'scopes'>;

const UNREACHABLE = 'Aegis cannot reach the engine. Check the engine status in the title bar.';
const LOAD_FAILED = 'Aegis could not read your scopes. Restart the engine and try again.';
const SAVE_FAILED = 'Aegis could not save that change. Try again.';
const UNREADABLE = 'The engine answered with something Aegis could not read.';

function bridge(): Bridge | undefined {
  return typeof window === 'undefined' ? undefined : window.aegis;
}

type Outcome<T> =
  { readonly ok: true; readonly value: T } | { readonly ok: false; readonly text: string };

async function settle<T>(
  call: () => Promise<BridgeResult<CoreResponse | null>>,
  parse: (body: unknown) => T | null,
  fallback: string,
): Promise<Outcome<T> | null> {
  let result: BridgeResult<CoreResponse | null>;
  try {
    result = await call();
  } catch {
    return { ok: false, text: fallback };
  }
  if (!result.ok) {
    return { ok: false, text: result.error.code === 'unavailable' ? UNREACHABLE : fallback };
  }
  if (result.value === null) return null; // the person cancelled the dialog
  const { status, body } = result.value;
  if (status < 200 || status >= 300)
    return { ok: false, text: coreDetail(status, body) ?? fallback };
  const parsed = parse(body);
  return parsed === null ? { ok: false, text: UNREADABLE } : { ok: true, value: parsed };
}

function core(b: Bridge, request: CoreRequest): () => Promise<BridgeResult<CoreResponse | null>> {
  return () => b.core.request(request);
}

function set(patch: Partial<ScopesState>): void {
  useScopesStore.setState(patch);
}

/** Put one scope's new state into the list, in name order. */
function upsert(scope: ScopeInfo): readonly ScopeInfo[] {
  const others = useScopesStore.getState().scopes.filter((item) => item.id !== scope.id);
  return [...others, scope].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
  );
}

export async function loadScopes(b: Bridge | undefined = bridge()): Promise<void> {
  if (b === undefined) return;
  set({ status: 'loading', loadError: null });
  const outcome = await settle(
    core(b, { method: 'GET', path: '/scopes' }),
    parseScopeList,
    LOAD_FAILED,
  );
  if (outcome === null || !outcome.ok) {
    set({ status: 'error', loadError: outcome?.text ?? LOAD_FAILED });
    return;
  }
  const { selectedId } = useScopesStore.getState();
  set({
    status: 'ready',
    scopes: outcome.value,
    loadError: null,
    selectedId: outcome.value.some((scope) => scope.id === selectedId) ? selectedId : null,
  });
}

/** Run one action with `busy` set, and report what happened. */
async function act<T>(
  busy: string,
  run: () => Promise<Outcome<T> | null>,
  done: (value: T) => { patch: Partial<ScopesState>; text: string },
): Promise<void> {
  set({ busy, notice: null });
  const outcome = await run();
  if (outcome === null) {
    set({ busy: null });
    return;
  }
  if (!outcome.ok) {
    set({ busy: null, notice: { tone: 'error', text: outcome.text } });
    return;
  }
  const { patch, text } = done(outcome.value);
  set({ ...patch, busy: null, notice: { tone: 'info', text } });
}

/** A new scope: the person names it, then picks its first folder in the dialog. */
export async function createScope(name: string, b: Bridge | undefined = bridge()): Promise<void> {
  if (b === undefined) return;
  await act(
    'create',
    () => settle(() => b.scopes.create(name), parseScope, SAVE_FAILED),
    (scope) => ({
      patch: { scopes: upsert(scope), selectedId: scope.id },
      text: `Made the scope ${scope.name}.`,
    }),
  );
}

/** One more folder, picked in the dialog. */
export async function addScopeFolder(
  scopeId: number,
  b: Bridge | undefined = bridge(),
): Promise<void> {
  if (b === undefined) return;
  await act(
    `scope:${String(scopeId)}`,
    () => settle(() => b.scopes.addFolder(scopeId), parseScope, SAVE_FAILED),
    (scope) => ({ patch: { scopes: upsert(scope) }, text: `Added a folder to ${scope.name}.` }),
  );
}

/** Rename, or keep fewer folders. Only ever narrows: the core refuses anything else. */
export async function updateScope(
  scope: ScopeInfo,
  change: { readonly name?: string; readonly folders?: readonly string[] },
  b: Bridge | undefined = bridge(),
): Promise<void> {
  if (b === undefined) return;
  const body = { name: change.name ?? scope.name, folders: change.folders ?? scope.folders };
  await act(
    `scope:${String(scope.id)}`,
    () =>
      settle(
        core(b, { method: 'PUT', path: `/scopes/${String(scope.id)}`, body }),
        parseScope,
        SAVE_FAILED,
      ),
    (saved) => ({
      patch: { scopes: upsert(saved) },
      text:
        change.folders !== undefined
          ? `Removed a folder from ${saved.name}. No files were changed.`
          : `Renamed the scope to ${saved.name}.`,
    }),
  );
}

/** Delete a scope. Only the scope goes; no file is touched. */
export async function deleteScope(
  scope: ScopeInfo,
  b: Bridge | undefined = bridge(),
): Promise<void> {
  if (b === undefined) return;
  await act(
    `scope:${String(scope.id)}`,
    () =>
      settle(
        core(b, { method: 'DELETE', path: `/scopes/${String(scope.id)}` }),
        parseScopeList,
        SAVE_FAILED,
      ),
    (scopes) => {
      const { selectedId } = useScopesStore.getState();
      return {
        patch: { scopes, selectedId: selectedId === scope.id ? null : selectedId },
        text: `Deleted the scope ${scope.name}. No files were changed.`,
      };
    },
  );
}

/** The scope the next task will run in; `null` for none. */
export function selectScope(scopeId: number | null): void {
  set({ selectedId: scopeId });
}
