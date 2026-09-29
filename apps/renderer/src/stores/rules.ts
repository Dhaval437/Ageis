import { create } from 'zustand';
import type { AllowRuleInfo } from '@aegis/shared';

/**
 * The always-allow rules, as the Rules screen lists them (`P3-18`). A store for the same
 * reason as `stores/scopes.ts`: every state is reachable from a story and a test. Its
 * only writer is `lib/rules-client.ts`.
 */

export type RulesStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface RulesNotice {
  readonly tone: 'error' | 'info';
  readonly text: string;
}

export interface RulesState {
  readonly status: RulesStatus;
  readonly rules: readonly AllowRuleInfo[];
  /** Why the list could not be loaded, in words for the person. */
  readonly loadError: string | null;
  /** The rule being revoked, if any. */
  readonly revoking: number | null;
  /** The outcome of the last revoke, announced politely. */
  readonly notice: RulesNotice | null;
}

export const INITIAL_RULES: RulesState = {
  status: 'idle',
  rules: [],
  loadError: null,
  revoking: null,
  notice: null,
};

export const useRulesStore = create<RulesState>()(() => ({ ...INITIAL_RULES }));
