import { create } from 'zustand';
import type { ScopeInfo } from '@aegis/shared';

/**
 * The scopes, as the titlebar picker and the Manage-scopes list show them (`P3-17`).
 *
 * A store rather than component state for the reason `stores/recovery.ts` gives: the
 * loading and error states must be reachable from a story and a test without a bridge
 * to stall. Its only writer is `lib/scopes-client.ts`.
 *
 * `selectedId` is the scope the next task will run in. Nothing starts a task yet
 * (`P4`); until then it is what the titlebar and the live view say.
 */

export type ScopesStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ScopesNotice {
  readonly tone: 'error' | 'info';
  readonly text: string;
}

export interface ScopesState {
  readonly status: ScopesStatus;
  readonly scopes: readonly ScopeInfo[];
  /** Why the list could not be loaded, in words for the person. */
  readonly loadError: string | null;
  readonly selectedId: number | null;
  /** What is in flight: `'create'`, or `scope:<id>` for one scope's action. */
  readonly busy: string | null;
  /** The outcome of the last action, announced politely. */
  readonly notice: ScopesNotice | null;
}

export const INITIAL_SCOPES: ScopesState = {
  status: 'idle',
  scopes: [],
  loadError: null,
  selectedId: null,
  busy: null,
  notice: null,
};

export const useScopesStore = create<ScopesState>()(() => ({ ...INITIAL_SCOPES }));

/** The selected scope, if it still exists. */
export function selectedScope(state: Pick<ScopesState, 'scopes' | 'selectedId'>): ScopeInfo | null {
  return state.scopes.find((scope) => scope.id === state.selectedId) ?? null;
}
