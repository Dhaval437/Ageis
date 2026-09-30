import { describe, expect, it, vi } from 'vitest';
import { APPROVAL_CHOICES, RULE_KINDS, type CoreStreamMessage } from '@aegis/shared';
import {
  INPUT_GUARD_MS,
  REPLAY_HOLD_MS,
  createApprovalVisibility,
  createPendingApprovals,
} from '../src/main/approval-policy.js';

/**
 * The approval window's decisions (P3-19): which questions are pending, which answers
 * MAIN lets through to the core, and when the window is on screen.
 */

let seq = 0;

function event(type: string, payload: unknown): CoreStreamMessage {
  seq += 1;
  return {
    kind: 'event',
    event: { seq, ts: '2026-09-30T12:00:00.000Z', task_id: '7', type, payload },
  };
}

const requested = (id: unknown): CoreStreamMessage =>
  event('approval.requested', { approval_id: id });
const resolved = (id: unknown): CoreStreamMessage =>
  event('approval.resolved', { approval_id: id, choice: 'deny' });

function setup() {
  let clock = 1_000;
  const pending = createPendingApprovals(() => clock);
  return {
    pending,
    advance: (ms: number) => {
      clock += ms;
    },
  };
}

describe('which questions are pending', () => {
  it('is pending from approval.requested until approval.resolved', () => {
    const { pending } = setup();
    expect(pending.count()).toBe(0);
    expect(pending.observe(requested(4))).toBe(true);
    expect(pending.ids()).toEqual([4]);
    expect(pending.observe(resolved(4))).toBe(true);
    expect(pending.count()).toBe(0);
  });

  it('keeps them oldest first, the order the dialog asks in', () => {
    const { pending } = setup();
    pending.observe(requested(9));
    pending.observe(requested(3));
    pending.observe(requested(5));
    pending.observe(resolved(3));
    expect(pending.ids()).toEqual([9, 5]);
  });

  it('reports no change for a repeat, a stranger, or another event', () => {
    const { pending } = setup();
    pending.observe(requested(4));
    expect(pending.observe(requested(4))).toBe(false);
    expect(pending.observe(resolved(8))).toBe(false);
    expect(pending.observe(event('task.status', { status: 'RUNNING' }))).toBe(false);
    expect(pending.observe({ kind: 'connection', state: 'live' })).toBe(false);
    expect(pending.observe({ kind: 'connection', state: 'connecting' })).toBe(false);
    expect(pending.ids()).toEqual([4]);
  });

  it.each([0, -1, 1.5, '4', null, undefined, 2 ** 60])('ignores the id %s', (id) => {
    const { pending } = setup();
    expect(pending.observe(requested(id))).toBe(false);
    expect(pending.count()).toBe(0);
  });

  it.each([
    null,
    'text',
    7,
    { type: 'approval.requested' },
    { type: 'approval.requested', payload: 3 },
  ])('ignores the malformed event %j', (malformed) => {
    const { pending } = setup();
    expect(pending.observe({ kind: 'event', event: malformed })).toBe(false);
    expect(pending.count()).toBe(0);
  });

  it('forgets every question on a reset: the replay asks again', () => {
    const { pending } = setup();
    pending.observe(requested(4));
    expect(pending.observe({ kind: 'reset' })).toBe(true);
    expect(pending.count()).toBe(0);
    expect(pending.observe({ kind: 'reset' })).toBe(false);
  });

  it.each(['down', 'unavailable'] as const)(
    'forgets every question when the core is %s: nobody is waiting on an answer',
    (state) => {
      const { pending } = setup();
      pending.observe(requested(4));
      expect(pending.observe({ kind: 'connection', state })).toBe(true);
      expect(pending.count()).toBe(0);
    },
  );

  it('stays bounded against a stream that only ever asks', () => {
    const { pending } = setup();
    for (let id = 1; id <= 500; id += 1) pending.observe(requested(id));
    expect(pending.count()).toBe(64);
    expect(pending.ids()[0]).toBe(1);
  });
});

describe('which answers MAIN lets through', () => {
  function asked(id = 4) {
    const made = setup();
    made.pending.observe(requested(id));
    made.advance(INPUT_GUARD_MS);
    return made;
  }

  it('passes a well-formed answer to a pending question', () => {
    const { pending } = asked();
    expect(pending.check({ approvalId: 4, choice: 'allow', rule: null })).toEqual({
      ok: true,
      answer: { approvalId: 4, choice: 'allow', rule: null },
    });
    expect(pending.check({ approvalId: 4, choice: 'deny', rule: null })).toEqual({
      ok: true,
      answer: { approvalId: 4, choice: 'deny', rule: null },
    });
  });

  it.each(RULE_KINDS)('passes allow_always with the rule kind %s', (rule) => {
    const { pending } = asked();
    expect(pending.check({ approvalId: 4, choice: 'allow_always', rule })).toEqual({
      ok: true,
      answer: { approvalId: 4, choice: 'allow_always', rule },
    });
  });

  it('carries nothing through but the three fields', () => {
    const { pending } = asked();
    const checked = pending.check({
      approvalId: 4,
      choice: 'allow_always',
      rule: 'tool_in_folder',
      folder: 'C:\\',
      path: '/approvals/5',
    });
    expect(checked).toEqual({
      ok: true,
      answer: { approvalId: 4, choice: 'allow_always', rule: 'tool_in_folder' },
    });
  });

  it.each(APPROVAL_CHOICES)('refuses %s for a question that is not pending', (choice) => {
    const { pending } = asked();
    const rule = choice === 'allow_always' ? 'exact' : null;
    const checked = pending.check({ approvalId: 5, choice, rule });
    expect(checked.ok).toBe(false);
    if (!checked.ok) expect(checked.message).toMatch(/already closed/);
  });

  it('refuses an answer once the question is resolved, reset away, or the core is gone', () => {
    for (const end of [resolved(4), { kind: 'reset' }, { kind: 'connection', state: 'down' }]) {
      const { pending } = asked();
      pending.observe(end as CoreStreamMessage);
      expect(pending.check({ approvalId: 4, choice: 'allow', rule: null }).ok).toBe(false);
    }
  });

  it.each([
    null,
    undefined,
    'allow',
    4,
    [],
    {},
    { approvalId: 4 },
    { approvalId: '4', choice: 'allow', rule: null },
    { approvalId: 4.5, choice: 'allow', rule: null },
    { approvalId: 0, choice: 'allow', rule: null },
    { approvalId: 4, choice: 'yes', rule: null },
    { approvalId: 4, choice: 'ALLOW', rule: null },
    { approvalId: 4, choice: 'allow', rule: 'exact' },
    { approvalId: 4, choice: 'deny', rule: 'exact' },
    { approvalId: 4, choice: 'allow_always', rule: null },
    { approvalId: 4, choice: 'allow_always' },
    { approvalId: 4, choice: 'allow_always', rule: 'everything' },
    { approvalId: 4, choice: 'allow_always', rule: { kind: 'exact' } },
  ])('refuses the malformed answer %j', (input) => {
    const { pending } = asked();
    expect(pending.check(input)).toEqual({ ok: false, message: 'That is not an answer.' });
  });

  it('refuses Allow inside the input guard, and passes it the moment the guard ends', () => {
    const { pending, advance } = setup();
    pending.observe(requested(4));
    advance(INPUT_GUARD_MS - 1);
    for (const [choice, rule] of [
      ['allow', null],
      ['allow_always', 'exact'],
    ] as const) {
      const early = pending.check({ approvalId: 4, choice, rule });
      expect(early.ok).toBe(false);
      if (!early.ok) expect(early.message).toMatch(/too soon/);
    }
    advance(1);
    expect(pending.check({ approvalId: 4, choice: 'allow', rule: null }).ok).toBe(true);
  });

  it('never refuses Deny for being early', () => {
    const { pending } = setup();
    pending.observe(requested(4));
    expect(pending.check({ approvalId: 4, choice: 'deny', rule: null }).ok).toBe(true);
  });

  it('guards each question from its own arrival, and again after a replay', () => {
    const { pending, advance } = setup();
    pending.observe(requested(4));
    advance(INPUT_GUARD_MS);
    pending.observe(requested(5));
    expect(pending.check({ approvalId: 4, choice: 'allow', rule: null }).ok).toBe(true);
    expect(pending.check({ approvalId: 5, choice: 'allow', rule: null }).ok).toBe(false);

    pending.observe({ kind: 'reset' });
    pending.observe(requested(4));
    expect(pending.check({ approvalId: 4, choice: 'allow', rule: null }).ok).toBe(false);
  });

  it('does not restart a question’s guard when the stream repeats it', () => {
    const { pending, advance } = setup();
    pending.observe(requested(4));
    advance(INPUT_GUARD_MS);
    pending.observe(requested(4));
    expect(pending.check({ approvalId: 4, choice: 'allow', rule: null }).ok).toBe(true);
  });
});

interface FakeTimer {
  readonly callback: () => void;
  readonly ms: number;
  cleared: boolean;
}

function visibilitySetup() {
  const timers: FakeTimer[] = [];
  const show = vi.fn();
  const hide = vi.fn();
  const visibility = createApprovalVisibility({
    show,
    hide,
    setTimer: (callback, ms) => {
      const timer: FakeTimer = { callback, ms, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (handle) => {
      (handle as FakeTimer).cleared = true;
    },
  });
  return {
    visibility,
    show,
    hide,
    timers,
    fire: () => {
      for (const timer of timers.splice(0)) if (!timer.cleared) timer.callback();
    },
  };
}

describe('when the approval window is on screen', () => {
  it('shows for a question and hides at once when the last one closes', () => {
    const { visibility, show, hide, timers } = visibilitySetup();
    visibility.update(1, false);
    expect(show).toHaveBeenCalledOnce();
    expect(hide).not.toHaveBeenCalled();
    visibility.update(0, false);
    expect(hide).toHaveBeenCalledOnce();
    expect(timers).toHaveLength(0);
  });

  it('comes forward again for each change while questions remain', () => {
    const { visibility, show, hide } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(2, false);
    visibility.update(1, false);
    expect(show).toHaveBeenCalledTimes(3);
    expect(hide).not.toHaveBeenCalled();
  });

  it('holds through a reset, and stays up when the replay asks again', () => {
    const { visibility, show, hide, timers, fire } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(0, true);
    expect(hide).not.toHaveBeenCalled();
    expect(timers.map((timer) => timer.ms)).toEqual([REPLAY_HOLD_MS]);
    visibility.update(1, false);
    fire();
    expect(hide).not.toHaveBeenCalled();
    expect(show).toHaveBeenCalledTimes(2);
  });

  it('hides after the hold when the replay does not ask again', () => {
    const { visibility, hide, fire } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(0, true);
    fire();
    expect(hide).toHaveBeenCalledOnce();
  });

  it('starts one hold, not one per reset', () => {
    const { visibility, timers } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(0, true);
    visibility.update(0, true);
    expect(timers).toHaveLength(1);
  });

  it('hides at once, hold or not, when the questions are gone for good', () => {
    const { visibility, hide, fire } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(0, true);
    visibility.update(0, false);
    expect(hide).toHaveBeenCalledOnce();
    fire();
    expect(hide).toHaveBeenCalledOnce();
  });

  it('cancels a hold on dispose', () => {
    const { visibility, hide, fire } = visibilitySetup();
    visibility.update(1, false);
    visibility.update(0, true);
    visibility.dispose();
    fire();
    expect(hide).not.toHaveBeenCalled();
  });
});
