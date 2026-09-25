import { create } from 'zustand';
import type { ApprovalChoice } from '@aegis/shared';

/**
 * What the approval dialog itself is doing (`P3-12`): which approval's buttons are
 * live yet, which answer is on its way, and what the last failed one had to say.
 *
 * *Which* approval is showing is not here — that is derived from the event stream
 * (`lib/approvals.ts`), so the dialog closes when the core says the question is
 * closed and at no other time. This store holds only the dialog's own state, and it
 * is a store rather than `useState` for the reason `recovery.ts` is one: a story and
 * a test must be able to reach the guarded, sending and failed states directly.
 */

export interface ApprovalUiSnapshot {
  /**
   * The approval whose *Allow* buttons have come out of the 200 ms input guard
   * (`UI.md § 5`). Any other id — a new approval — starts guarded again.
   */
  readonly armed: number | null;
  /** The answer on its way to the core, or `null`. */
  readonly sending: { readonly id: number; readonly choice: ApprovalChoice } | null;
  /** Why the last answer did not land, for the approval it was about. */
  readonly notice: { readonly id: number; readonly text: string } | null;
}

export const INITIAL_APPROVAL_UI: ApprovalUiSnapshot = { armed: null, sending: null, notice: null };

export interface ApprovalUiStore extends ApprovalUiSnapshot {
  readonly arm: (id: number) => void;
  readonly begin: (id: number, choice: ApprovalChoice) => void;
  readonly settle: (id: number, text: string | null) => void;
}

export const useApprovalStore = create<ApprovalUiStore>()((set) => ({
  ...INITIAL_APPROVAL_UI,
  arm: (id) => {
    set({ armed: id });
  },
  begin: (id, choice) => {
    set({ sending: { id, choice }, notice: null });
  },
  settle: (id, text) => {
    set({ sending: null, notice: text === null ? null : { id, text } });
  },
}));
