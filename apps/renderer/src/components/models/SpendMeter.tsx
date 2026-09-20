import type { ReactElement } from 'react';
import type { BudgetLimitsSpec, SpendTotals } from '@aegis/shared';
import {
  fillRatio,
  formatMoney,
  formatTokens,
  spendLabel,
  spendTone,
  type SpendTone,
} from '@/lib/spend';
import { cn } from '@/lib/utils';

/**
 * Today's spend against today's ceiling (`UI.md § 8.4`, `UI.md § 12`).
 *
 * Pure: it is handed the totals and the limits and renders them. Where the
 * numbers come from — the first from `GET /v1/models/spend`, every one after
 * from `cost.updated` — is `ModelsScreen`'s question, so this can be shown in
 * every state without a bridge.
 *
 * Three things it refuses to do. It never shows a bar with no ceiling behind it,
 * because a bar with no maximum means nothing; it says *at least* whenever the
 * total is missing a price (`P1-09`); and it states the condition in **words as
 * well as colour** (`REVIEW.md § 3`), because amber alone is not a message.
 */

const TONE_BAR: Record<SpendTone, string> = {
  normal: 'bg-accent',
  near: 'bg-caution',
  over: 'bg-danger',
};

const TONE_TEXT: Record<SpendTone, string> = {
  normal: 'text-text-dim',
  near: 'text-caution',
  over: 'text-danger',
};

const TONE_NOTE: Record<SpendTone, string | null> = {
  normal: null,
  near: 'Close to your daily limit.',
  over: 'Daily limit reached. Tasks will pause until you raise it.',
};

export interface SpendMeterProps {
  /** Today's totals, or `null` while they are still being read. */
  readonly day: SpendTotals | null;
  readonly limits: BudgetLimitsSpec;
}

export function SpendMeter({ day, limits }: SpendMeterProps): ReactElement {
  if (day === null) {
    return (
      <section
        aria-label="Spend today"
        className="rounded-card border border-border bg-surface-1 p-4"
      >
        <p className="text-sm text-text-dim">Reading today&rsquo;s spend…</p>
        <div className="mt-3 h-1.5 w-full animate-pulse rounded-pill bg-surface-2" />
      </section>
    );
  }

  const ratio = fillRatio(day.cents, limits.day_cents);
  const tone = spendTone(ratio);
  const note = TONE_NOTE[tone];
  const limit =
    limits.day_cents === null ? 'No daily limit' : `of ${formatMoney(limits.day_cents)}`;

  return (
    <section
      aria-label="Spend today"
      className="rounded-card border border-border bg-surface-1 p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-base font-medium text-text">Today</h3>
        <p className={cn('text-base tabular-nums', TONE_TEXT[tone])}>
          <span className="font-medium">{spendLabel(day)}</span>{' '}
          <span className="text-text-dim">{limit}</span>
        </p>
      </div>

      {ratio === null ? null : (
        <div
          role="progressbar"
          aria-label="Spend against your daily limit"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(ratio * 100)}
          className="mt-3 h-1.5 w-full overflow-hidden rounded-pill bg-surface-2"
        >
          <div
            className={cn(
              'h-full rounded-pill transition-[width] duration-120 ease-out',
              TONE_BAR[tone],
            )}
            style={{ width: `${String(Math.round(ratio * 100))}%` }}
          />
        </div>
      )}

      <p className="mt-2 text-sm text-text-dim">
        {formatTokens(day.tokens)} tokens
        {day.unpriced_calls > 0
          ? ` · ${String(day.unpriced_calls)} call${day.unpriced_calls === 1 ? '' : 's'} with no published price`
          : ''}
      </p>

      {note !== null && (
        <p className={cn('mt-2 text-sm', TONE_TEXT[tone])} role="status">
          {note}
        </p>
      )}
    </section>
  );
}
