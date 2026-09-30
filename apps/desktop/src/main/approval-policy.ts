/**
 * The approval window's decisions (P3-19), with no Electron in them.
 *
 * MAIN opens the approval window when the core asks a question and closes it when no
 * question is left (`UI.md § 3`, § 5). It learns both from the stream it already
 * forwards — `approval.requested` and `approval.resolved`, the second of which the
 * core publishes exactly once for every question however it ends — so the window is
 * a function of the stream, like every other screen (invariant 15).
 *
 * MAIN also checks every answer the window sends, because the window's page shows
 * text scraped off the user's screen and is treated as a hostile caller:
 *
 * - the id must be a question that is **pending right now** — no answering ahead of
 *   a question, or after it closed;
 * - the choice must be one of the three, and *Allow always* must name a rule kind;
 * - **Allow is refused for 200 ms after MAIN first saw the question**, the same input
 *   guard the dialog keeps. The dialog's guard is the real one; this one holds if the
 *   page's does not. Deny is never refused.
 *
 * An answer that passes is the only thing MAIN signs for the core (`x-aegis-grant`),
 * and the core takes an *Allow* from nowhere else — so the main window's generic
 * `core.request` can deny a question but never allow one.
 */

import {
  APPROVAL_CHOICES,
  RULE_KINDS,
  type ApprovalChoice,
  type CoreStreamMessage,
  type RuleKind,
} from '@aegis/shared';

/** `UI.md § 5`: how long Allow stays refused after a question appears. */
export const INPUT_GUARD_MS = 200;

/**
 * Far more questions than one core asks at once (it asks one per step). A stream that
 * claims more is broken; the set stays bounded (`REVIEW.md § 2`).
 */
const MAX_PENDING = 64;

export interface ApprovalAnswer {
  readonly approvalId: number;
  readonly choice: ApprovalChoice;
  readonly rule: RuleKind | null;
}

export type AnswerCheck =
  | { readonly ok: true; readonly answer: ApprovalAnswer }
  | { readonly ok: false; readonly message: string };

export interface PendingApprovals {
  /** Feed every message MAIN forwards, in order. Returns whether the set changed. */
  readonly observe: (message: CoreStreamMessage) => boolean;
  /** How many questions are pending. */
  readonly count: () => number;
  /** Pending question ids, oldest first — the order the dialog shows them in. */
  readonly ids: () => readonly number[];
  /** Validate one answer from the window against what is pending now. */
  readonly check: (input: unknown) => AnswerCheck;
}

function isChoice(value: unknown): value is ApprovalChoice {
  return typeof value === 'string' && (APPROVAL_CHOICES as readonly string[]).includes(value);
}

function isRuleKind(value: unknown): value is RuleKind {
  return typeof value === 'string' && (RULE_KINDS as readonly string[]).includes(value);
}

function isApprovalId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
}

/** `{type, approvalId}` for an approval event, or `null` for anything else. */
function approvalEvent(
  event: unknown,
): { type: 'approval.requested' | 'approval.resolved'; approvalId: number } | null {
  if (typeof event !== 'object' || event === null) return null;
  const { type, payload } = event as Record<string, unknown>;
  if (type !== 'approval.requested' && type !== 'approval.resolved') return null;
  if (typeof payload !== 'object' || payload === null) return null;
  const { approval_id: approvalId } = payload as Record<string, unknown>;
  return isApprovalId(approvalId) ? { type, approvalId } : null;
}

/** `now` is a monotonic clock in milliseconds (`performance.now`). */
export function createPendingApprovals(now: () => number): PendingApprovals {
  // Insertion order is arrival order, which is the order the dialog asks in.
  const pending = new Map<number, number>();

  return {
    observe: (message) => {
      if (
        message.kind === 'reset' ||
        (message.kind === 'connection' &&
          (message.state === 'down' || message.state === 'unavailable'))
      ) {
        // A reset: a new core, or a replay from scratch that will say it all again.
        // No core: its questions went with it, and nobody is waiting on an answer.
        const had = pending.size > 0;
        pending.clear();
        return had;
      }
      if (message.kind !== 'event') return false;
      const update = approvalEvent(message.event);
      if (update === null) return false;
      if (update.type === 'approval.resolved') return pending.delete(update.approvalId);
      if (pending.has(update.approvalId) || pending.size >= MAX_PENDING) return false;
      pending.set(update.approvalId, now());
      return true;
    },
    count: () => pending.size,
    ids: () => [...pending.keys()],
    check: (input) => {
      if (typeof input !== 'object' || input === null) {
        return { ok: false, message: 'That is not an answer.' };
      }
      const { approvalId, choice, rule } = input as Record<string, unknown>;
      if (!isApprovalId(approvalId) || !isChoice(choice)) {
        return { ok: false, message: 'That is not an answer.' };
      }
      if (choice === 'allow_always' ? !isRuleKind(rule) : rule !== null && rule !== undefined) {
        return { ok: false, message: 'That is not an answer.' };
      }
      const seenAt = pending.get(approvalId);
      if (seenAt === undefined) {
        return {
          ok: false,
          message:
            'This question is already closed: it was answered, or it timed out and was denied.',
        };
      }
      if (choice !== 'deny' && now() - seenAt < INPUT_GUARD_MS) {
        return { ok: false, message: 'That was too soon after the question appeared. Try again.' };
      }
      return {
        ok: true,
        answer: { approvalId, choice, rule: choice === 'allow_always' ? (rule as RuleKind) : null },
      };
    },
  };
}

/**
 * A reset is followed by a replay that asks every open question again, once MAIN has
 * reopened the stream. The window stays up this long after one, so a page load does not
 * blink it away and take the focus a second time. Measured in the built app: the HUD's
 * first load resets the stream just as the first question arrives, and the replay came
 * ~300 ms later — 250 ms was too short, and the window blinked.
 */
export const REPLAY_HOLD_MS = 1_000;

export interface ApprovalVisibilityOptions {
  /** Show the window over everything and ask for the focus. */
  readonly show: () => void;
  readonly hide: () => void;
  readonly holdMs?: number;
  readonly setTimer?: (callback: () => void, ms: number) => unknown;
  readonly clearTimer?: (handle: unknown) => void;
}

export interface ApprovalVisibility {
  /**
   * Call whenever the pending set changed. `replaying` says a `reset` emptied it, so
   * the questions may be back in a moment.
   */
  readonly update: (pending: number, replaying: boolean) => void;
  readonly dispose: () => void;
}

/**
 * When the approval window is on screen: while a question is pending, and at no other
 * time. `electron`-free, like `hud-visibility.ts`: the window and the timers are
 * injected.
 */
export function createApprovalVisibility(options: ApprovalVisibilityOptions): ApprovalVisibility {
  const holdMs = options.holdMs ?? REPLAY_HOLD_MS;
  const setTimer =
    options.setTimer ?? ((callback: () => void, ms: number): unknown => setTimeout(callback, ms));
  const clearTimer =
    options.clearTimer ??
    ((handle: unknown): void => {
      clearTimeout(handle as NodeJS.Timeout);
    });
  let hold: unknown = null;

  function cancelHold(): void {
    if (hold !== null) clearTimer(hold);
    hold = null;
  }

  return {
    update: (pending, replaying) => {
      if (pending > 0) {
        cancelHold();
        options.show();
        return;
      }
      if (!replaying) {
        // The last question closed: answered, timed out, or stopped.
        cancelHold();
        options.hide();
        return;
      }
      hold ??= setTimer(() => {
        hold = null;
        options.hide();
      }, holdMs);
    },
    dispose: cancelHold,
  };
}
