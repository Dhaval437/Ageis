import type { SpendTotals, StreamEvent } from '@aegis/shared';
import { parseSpendTotals } from '@/lib/models-api';

/**
 * Turning the event stream into the number the spend meter shows
 * (`UI.md § 8.4`, `UI.md § 12`'s `SpendMeter`).
 *
 * `GET /v1/models/spend` gives the meter its first value, because a screen just
 * opened has seen no events. Every value after that comes from `cost.updated`
 * (`ARCHITECTURE.md § 9.2`), which the core publishes per model call — so the
 * meter is a function of the stream, as invariant 15 requires, and not of a poll.
 */

/**
 * The day total from the most recent `cost.updated`, or `null` if the stream has
 * not carried one. Searched backwards, because the last one is the current one.
 */
export function latestDaySpend(events: readonly StreamEvent[]): SpendTotals | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event === undefined || event.type !== 'cost.updated') continue;
    const day = parseSpendTotals(event.payload['day']);
    if (day !== null) return day;
  }
  return null;
}

/**
 * Cents as dollars, with enough precision to be honest about a small amount.
 *
 * Deliberately the same rule as the core's (`models/budget.py`): two decimals
 * rounds every sub-cent figure to `$0.01`, which once showed a spend of 0.75
 * cents and a limit of 0.5 cents as the same number. A cheap call really does
 * cost fractions of a cent.
 */
export function formatMoney(cents: number): string {
  const dollars = cents / 100;
  return cents >= 10 ? `$${dollars.toFixed(2)}` : `$${dollars.toFixed(4)}`;
}

/** `1,200` — grouped, because a token count is read, not calculated with. */
export function formatTokens(tokens: number): string {
  return tokens.toLocaleString('en-US');
}

/**
 * How full the bar is, 0 to 1. `null` when there is no ceiling to measure
 * against — a bar with no maximum is a decoration, and the meter shows the
 * number instead.
 */
export function fillRatio(spent: number, limit: number | null): number | null {
  if (limit === null || limit <= 0) return null;
  return Math.max(0, Math.min(1, spent / limit));
}

/**
 * What a total says about itself. A total missing a price says *at least*
 * (`P1-09`): presenting a short number as a complete one is the one thing the
 * budget guard's arithmetic must never let the UI do.
 */
export function spendLabel(day: SpendTotals): string {
  const money = formatMoney(day.cents);
  return day.unpriced_calls === 0 ? money : `at least ${money}`;
}

/** The tone the bar and the figure take. Amber before the ceiling, red at it. */
export type SpendTone = 'normal' | 'near' | 'over';

/** Amber from four fifths of the ceiling: the last chance to raise it in time. */
export const NEAR_LIMIT = 0.8;

export function spendTone(ratio: number | null): SpendTone {
  if (ratio === null) return 'normal';
  if (ratio >= 1) return 'over';
  return ratio >= NEAR_LIMIT ? 'near' : 'normal';
}
