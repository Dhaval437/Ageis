import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '@aegis/shared';
import {
  fillRatio,
  formatMoney,
  formatTokens,
  latestDaySpend,
  spendLabel,
  spendTone,
} from '@/lib/spend';

/**
 * The spend meter is a function of the stream (invariant 15), and of arithmetic
 * that must never be wrong in the cheap direction: a sub-cent figure is not
 * rounded into its own limit, and a total missing a price says so.
 */

function costEvent(seq: number, cents: number, unpriced = 0): StreamEvent {
  return {
    seq,
    ts: '2026-09-20T12:00:00.000Z',
    task_id: null,
    type: 'cost.updated',
    payload: {
      day: { cents, tokens: cents * 100, unpriced_calls: unpriced },
      task: { cents: 0, tokens: 0, unpriced_calls: 0 },
      limits: { task_cents: 100, day_cents: 1000, task_tokens: null, day_tokens: null },
      breach: null,
    },
  };
}

function otherEvent(seq: number): StreamEvent {
  return { seq, ts: '2026-09-20T12:00:00.000Z', task_id: null, type: 'log', payload: {} };
}

describe('latestDaySpend', () => {
  it('finds nothing in a stream that has carried no cost', () => {
    expect(latestDaySpend([otherEvent(1), otherEvent(2)])).toBeNull();
  });

  it('takes the most recent cost event, not the first', () => {
    const events = [costEvent(1, 10), otherEvent(2), costEvent(3, 42.5)];
    expect(latestDaySpend(events)?.cents).toBe(42.5);
  });

  it('ignores a cost event whose payload it cannot read', () => {
    const broken: StreamEvent = { ...costEvent(3, 0), payload: { day: 'quite a lot' } };
    expect(latestDaySpend([costEvent(1, 10), broken])?.cents).toBe(10);
  });
});

describe('formatMoney', () => {
  it('shows fractions of a cent below ten cents', () => {
    // The live-run bug: 0.75 cents and a limit of 0.5 cents both read `$0.01`.
    expect(formatMoney(0.75)).toBe('$0.0075');
    expect(formatMoney(0.5)).toBe('$0.0050');
    expect(formatMoney(0.75)).not.toBe(formatMoney(0.5));
  });

  it('shows two decimals from ten cents up', () => {
    expect(formatMoney(10)).toBe('$0.10');
    expect(formatMoney(1000)).toBe('$10.00');
  });

  it('matches the core’s rule at the boundary', () => {
    expect(formatMoney(9.9999)).toBe('$0.1000');
    expect(formatMoney(10.0)).toBe('$0.10');
  });
});

describe('formatTokens', () => {
  it('groups so a long number can be read at a glance', () => {
    expect(formatTokens(1_234_567)).toBe('1,234,567');
  });
});

describe('fillRatio', () => {
  it('is null with no ceiling, because a bar with no maximum means nothing', () => {
    expect(fillRatio(500, null)).toBeNull();
    expect(fillRatio(500, 0)).toBeNull();
  });

  it('clamps rather than overflowing the bar', () => {
    expect(fillRatio(1500, 1000)).toBe(1);
    expect(fillRatio(-5, 1000)).toBe(0);
  });
});

describe('spendTone', () => {
  it('is ordinary well under the ceiling', () => {
    expect(spendTone(0.5)).toBe('normal');
    expect(spendTone(null)).toBe('normal');
  });

  it('warns before the ceiling and not only at it', () => {
    expect(spendTone(0.8)).toBe('near');
    expect(spendTone(0.99)).toBe('near');
  });

  it('is over at the ceiling, because that is where a task pauses', () => {
    expect(spendTone(1)).toBe('over');
  });
});

describe('spendLabel', () => {
  it('states a complete total plainly', () => {
    expect(spendLabel({ cents: 1000, tokens: 0, unpriced_calls: 0 })).toBe('$10.00');
  });

  it('says *at least* when a price is missing', () => {
    expect(spendLabel({ cents: 1000, tokens: 0, unpriced_calls: 3 })).toBe('at least $10.00');
  });
});
