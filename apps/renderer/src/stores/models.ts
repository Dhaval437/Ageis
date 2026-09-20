import { create } from 'zustand';
import type {
  ModelCatalog,
  ModelSettings,
  ProviderId,
  SpendResponse,
  ValidateResponse,
} from '@aegis/shared';

/**
 * What the Models screen (`UI.md § 8.4`) is showing and doing.
 *
 * It is a store rather than component state for the reason `stores/recovery.ts`
 * gives: `REVIEW.md § 3` wants a loading story and an error story, and neither
 * is reachable in Storybook or a test without a bridge to stall. Every state
 * below is set the same way a story sets it.
 *
 * It holds two copies of the settings on purpose. `saved` is what the core has;
 * `draft` is what the user has typed. The difference is what the *Save* button
 * is for, and a screen that edited `saved` in place could not tell the user
 * there is anything to save.
 *
 * **It never holds a key.** `keyDrafts` holds what is being typed into a key
 * field right now, and is cleared the moment it is sent — the core answers with
 * `masked_key` and that is the only form of a saved key that exists here
 * (`ARCHITECTURE.md § 5.3`).
 */

export interface ModelsNotice {
  readonly tone: 'ok' | 'error';
  readonly text: string;
}

/** Where the screen is in its one load. `error` is the whole screen failing. */
export type ModelsStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ModelsSnapshot {
  readonly status: ModelsStatus;
  /** Why the load failed. Only set with `status: 'error'`. */
  readonly error: string | null;
  readonly catalog: ModelCatalog | null;
  /** What the core has stored. */
  readonly saved: ModelSettings | null;
  /** What the user has changed on screen but not saved. */
  readonly draft: ModelSettings | null;
  /** Today's spend as of the load; `cost.updated` keeps the meter live after it. */
  readonly spend: SpendResponse | null;
  readonly saving: boolean;
  /** The outcome of the last action, or `null` when there is nothing to say. */
  readonly notice: ModelsNotice | null;
  /** Which provider's *Test* is in flight. */
  readonly testing: ProviderId | null;
  /** The last *Test* result per provider. */
  readonly results: Readonly<Partial<Record<ProviderId, ValidateResponse>>>;
  /** Which provider's key is being written. */
  readonly savingKey: ProviderId | null;
  /** Unsaved text in a key field. Never a saved key. */
  readonly keyDrafts: Readonly<Partial<Record<ProviderId, string>>>;
}

export const INITIAL_MODELS: ModelsSnapshot = {
  status: 'idle',
  error: null,
  catalog: null,
  saved: null,
  draft: null,
  spend: null,
  saving: false,
  notice: null,
  testing: null,
  results: {},
  savingKey: null,
  keyDrafts: {},
};

export interface ModelsLoaded {
  readonly catalog: ModelCatalog;
  readonly settings: ModelSettings;
  readonly spend: SpendResponse;
}

export interface ModelsStore extends ModelsSnapshot {
  readonly beginLoad: () => void;
  readonly loaded: (data: ModelsLoaded) => void;
  readonly failed: (message: string) => void;
  /**
   * Change the draft.
   *
   * It takes an updater rather than a value so two edits in the same React batch
   * both land: a handler that closed over the `draft` it was rendered with would
   * overwrite the other one's change with a stale copy. Found by driving the
   * real app, where three role cards were given a model in one tick and only the
   * last survived.
   */
  readonly edit: (update: (draft: ModelSettings) => ModelSettings) => void;
  /** Throw away every unsaved change. */
  readonly revert: () => void;
  readonly beginSave: () => void;
  readonly settled: (settings: ModelSettings | null, notice: ModelsNotice | null) => void;
  readonly beginTest: (provider: ProviderId) => void;
  readonly tested: (provider: ProviderId, result: ValidateResponse | null) => void;
  readonly typeKey: (provider: ProviderId, key: string) => void;
  readonly beginKeySave: (provider: ProviderId) => void;
  readonly keySettled: (
    provider: ProviderId,
    catalog: ModelCatalog | null,
    notice: ModelsNotice,
  ) => void;
  readonly notify: (notice: ModelsNotice | null) => void;
}

/** True when the draft differs from what the core has. Drives the Save button. */
export function hasUnsavedChanges(state: ModelsSnapshot): boolean {
  if (state.draft === null || state.saved === null) return false;
  return JSON.stringify(state.draft) !== JSON.stringify(state.saved);
}

export const useModelsStore = create<ModelsStore>()((set) => ({
  ...INITIAL_MODELS,
  beginLoad: () => {
    set({ status: 'loading', error: null });
  },
  loaded: ({ catalog, settings, spend }) => {
    set({
      status: 'ready',
      error: null,
      catalog,
      saved: settings,
      draft: settings,
      spend,
    });
  },
  failed: (message) => {
    set({ status: 'error', error: message });
  },
  edit: (update) => {
    set((state) => (state.draft === null ? state : { draft: update(state.draft), notice: null }));
  },
  revert: () => {
    set((state) => ({ draft: state.saved, notice: null }));
  },
  beginSave: () => {
    set({ saving: true, notice: null });
  },
  settled: (settings, notice) => {
    set((state) =>
      settings === null
        ? { saving: false, notice }
        : {
            saving: false,
            notice,
            saved: settings,
            draft: settings,
            spend: spendWith(state, settings),
          },
    );
  },
  beginTest: (provider) => {
    set({ testing: provider, notice: null });
  },
  tested: (provider, result) => {
    set((state) => ({
      testing: null,
      results: result === null ? state.results : { ...state.results, [provider]: result },
      notice:
        result === null
          ? { tone: 'error', text: 'Aegis could not reach the engine to run that test.' }
          : null,
    }));
  },
  typeKey: (provider, key) => {
    set((state) => ({ keyDrafts: { ...state.keyDrafts, [provider]: key }, notice: null }));
  },
  beginKeySave: (provider) => {
    set({ savingKey: provider, notice: null });
  },
  keySettled: (provider, catalog, notice) => {
    set((state) => {
      const keyDrafts = { ...state.keyDrafts };
      if (catalog !== null) delete keyDrafts[provider];
      return {
        savingKey: null,
        notice,
        keyDrafts,
        catalog: catalog ?? state.catalog,
      };
    });
  },
  notify: (notice) => {
    set({ notice });
  },
}));

/**
 * The ceilings the meter is drawn against, after a save.
 *
 * The spend *total* still comes from the core and the stream; only the limits
 * moved, and they moved because the user just set them. Leaving the old ones
 * would show a bar measured against a number that is no longer true.
 */
function spendWith(state: ModelsSnapshot, settings: ModelSettings): SpendResponse | null {
  return state.spend === null ? null : { ...state.spend, limits: settings.limits };
}
