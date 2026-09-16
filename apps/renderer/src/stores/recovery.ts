import { create } from 'zustand';
import type { RecoveryAction, RecoveryNotice } from '@/lib/engine-recovery';

/**
 * What the Engine-unavailable screen is doing right now (P0-17): which of its
 * three actions is in flight, and what the last one had to say.
 *
 * It is a store rather than `useState` for the same reason the stream and window
 * state are: every state this screen can be in has to be reachable from a
 * Storybook story and a test without clicking through a bridge that is not
 * there. `REVIEW.md § 3` wants a loading and an error story, and a faked one
 * would prove nothing.
 */

export interface RecoverySnapshot {
  /** The action waiting on MAIN, or `null` when the screen is idle. */
  readonly busy: RecoveryAction | null;
  /** The outcome of the last action, or `null` when there is nothing to say. */
  readonly notice: RecoveryNotice | null;
}

export const INITIAL_RECOVERY: RecoverySnapshot = { busy: null, notice: null };

export interface RecoveryStore extends RecoverySnapshot {
  /** Marks an action in flight and clears the previous outcome. */
  readonly begin: (action: RecoveryAction) => void;
  readonly settle: (notice: RecoveryNotice | null) => void;
}

export const useRecoveryStore = create<RecoveryStore>()((set) => ({
  ...INITIAL_RECOVERY,
  begin: (action) => {
    set({ busy: action, notice: null });
  },
  settle: (notice) => {
    set({ busy: null, notice });
  },
}));
