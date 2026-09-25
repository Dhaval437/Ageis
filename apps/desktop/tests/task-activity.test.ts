import { describe, expect, it } from 'vitest';
import { TASK_STATES, type CoreStreamMessage } from '@aegis/shared';
import { createTaskActivity } from '../src/main/task-activity.js';

let seq = 0;

function status(taskId: unknown, value: unknown): CoreStreamMessage {
  seq += 1;
  return {
    kind: 'event',
    event: {
      seq,
      ts: '2026-09-25T12:00:00.000Z',
      task_id: taskId,
      type: 'task.status',
      payload: { status: value },
    },
  };
}

describe('createTaskActivity', () => {
  it('is idle until a task reports RUNNING, and idle again once it leaves it', () => {
    const activity = createTaskActivity();
    expect(activity.running()).toBe(false);

    activity.observe(status('1', 'QUEUED'));
    expect(activity.running()).toBe(false);
    activity.observe(status('1', 'RUNNING'));
    expect(activity.running()).toBe(true);
    activity.observe(status('1', 'DONE'));
    expect(activity.running()).toBe(false);
  });

  it.each(TASK_STATES.filter((state) => state !== 'RUNNING'))(
    'counts %s as not running',
    (state) => {
      const activity = createTaskActivity();
      activity.observe(status('1', 'RUNNING'));
      activity.observe(status('1', state));
      expect(activity.running()).toBe(false);
    },
  );

  it('comes back when a paused task resumes', () => {
    const activity = createTaskActivity();
    activity.observe(status('1', 'RUNNING'));
    activity.observe(status('1', 'PAUSED_BY_USER'));
    activity.observe(status('1', 'RUNNING'));
    expect(activity.running()).toBe(true);
  });

  it('stays running while any one task is', () => {
    const activity = createTaskActivity();
    activity.observe(status('1', 'RUNNING'));
    activity.observe(status('2', 'RUNNING'));
    activity.observe(status('1', 'STOPPED'));
    expect(activity.running()).toBe(true);
    activity.observe(status('2', 'FAILED'));
    expect(activity.running()).toBe(false);
  });

  it('forgets everything on a reset, which means a new core or a full replay', () => {
    const activity = createTaskActivity();
    activity.observe(status('1', 'RUNNING'));
    activity.observe({ kind: 'reset' });
    expect(activity.running()).toBe(false);
  });

  it('keeps what it knows when the connection drops, because a hung core still said it', () => {
    const activity = createTaskActivity();
    activity.observe(status('1', 'RUNNING'));
    activity.observe({ kind: 'connection', state: 'connecting' });
    activity.observe({ kind: 'connection', state: 'down' });
    expect(activity.running()).toBe(true);
  });

  it.each([
    ['a null event', null],
    ['another event type', { type: 'step.started', task_id: '1', payload: { status: 'RUNNING' } }],
    ['no task id', { type: 'task.status', task_id: null, payload: { status: 'RUNNING' } }],
    ['an empty task id', { type: 'task.status', task_id: '', payload: { status: 'RUNNING' } }],
    ['a numeric task id', { type: 'task.status', task_id: 1, payload: { status: 'RUNNING' } }],
    ['no payload', { type: 'task.status', task_id: '1', payload: null }],
    ['an unknown status', { type: 'task.status', task_id: '1', payload: { status: 'running' } }],
  ])('ignores %s', (_why, event) => {
    const activity = createTaskActivity();
    activity.observe({ kind: 'event', event });
    expect(activity.running()).toBe(false);
  });

  it('does not let a malformed event end a running task', () => {
    const activity = createTaskActivity();
    activity.observe(status('1', 'RUNNING'));
    activity.observe(status('1', 'done'));
    expect(activity.running()).toBe(true);
  });

  it('stays bounded, and answers running while full, when a stream claims too many tasks', () => {
    const activity = createTaskActivity();
    for (let task = 0; task < 10_000; task += 1) activity.observe(status(String(task), 'RUNNING'));
    expect(activity.running()).toBe(true);
    // Only the first 64 were kept: ending those empties it, so nothing grew past the cap.
    for (let task = 0; task < 64; task += 1) activity.observe(status(String(task), 'DONE'));
    expect(activity.running()).toBe(false);
    activity.observe(status('late', 'RUNNING'));
    expect(activity.running()).toBe(true);
  });
});
