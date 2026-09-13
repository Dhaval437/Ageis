import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AegisBridge, CoreStreamMessage } from '@aegis/shared';
import { engineStatusView } from '@/lib/engine-status';
import { connectStream } from '@/lib/stream-client';
import { parseStreamEvent, parseStreamMessage } from '@/lib/stream-event';
import {
  INITIAL_STREAM,
  MAX_EVENTS,
  reduceStream,
  useStreamStore,
  type StreamSnapshot,
} from '@/stores/stream';

function event(seq: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    seq,
    ts: '2026-09-14T09:00:00.000+00:00',
    task_id: 't-1',
    type: 'task.status',
    payload: { status: 'running' },
    ...overrides,
  };
}

const wrap = (value: unknown): CoreStreamMessage => ({ kind: 'event', event: value });

const applyAll = (messages: unknown[], from: StreamSnapshot = INITIAL_STREAM): StreamSnapshot =>
  messages.reduce<StreamSnapshot>((state, message) => reduceStream(state, message), from);

describe('parseStreamEvent', () => {
  it('accepts a well-formed § 9.2 event', () => {
    expect(parseStreamEvent(event(1))).toEqual(event(1));
    expect(parseStreamEvent(event(2, { task_id: null }))?.task_id).toBeNull();
  });

  it.each([
    ['a non-object', 'seq=1'],
    ['a zero seq', event(0)],
    ['a fractional seq', event(1.5)],
    ['a string seq', event(1, { seq: '1' })],
    ['a missing ts', event(1, { ts: undefined })],
    ['a numeric task_id', event(1, { task_id: 7 })],
    ['an undocumented type', event(1, { type: 'task.exploded' })],
    ['an array payload', event(1, { payload: [] })],
    ['a null payload', event(1, { payload: null })],
  ])('rejects %s', (_label, value) => {
    expect(parseStreamEvent(value)).toBeNull();
  });
});

describe('parseStreamMessage', () => {
  it('reads each envelope kind', () => {
    expect(parseStreamMessage({ kind: 'reset' })).toEqual({ kind: 'reset' });
    expect(parseStreamMessage({ kind: 'connection', state: 'live' })).toEqual({
      kind: 'connection',
      state: 'live',
    });
    expect(parseStreamMessage(wrap(event(1))).kind).toBe('event');
  });

  it.each([null, 'reset', { kind: 'nope' }, { kind: 'connection', state: 'asleep' }, wrap({})])(
    'marks %j invalid',
    (value) => {
      expect(parseStreamMessage(value)).toEqual({ kind: 'invalid' });
    },
  );
});

describe('reduceStream', () => {
  it('starts empty, with no connection state until MAIN speaks', () => {
    expect(INITIAL_STREAM).toMatchObject({ connection: null, lastSeq: 0, events: [] });
  });

  it('applies events in seq order', () => {
    const state = applyAll([wrap(event(1)), wrap(event(2))]);
    expect(state.events.map((e) => e.seq)).toEqual([1, 2]);
    expect(state.lastSeq).toBe(2);
  });

  it('never applies an event twice or out of order', () => {
    const state = applyAll([wrap(event(1)), wrap(event(2)), wrap(event(2)), wrap(event(1))]);
    expect(state.events.map((e) => e.seq)).toEqual([1, 2]);
  });

  it('counts an invalid message and changes nothing else', () => {
    const before = applyAll([wrap(event(1))]);
    const after = reduceStream(before, wrap(event(2, { type: 'task.exploded' })));
    expect(after.events).toBe(before.events);
    expect(after.rejected).toBe(1);
  });

  it('reset drops everything derived from the stream but keeps the connection', () => {
    const state = applyAll([
      { kind: 'connection', state: 'live' },
      wrap(event(1)),
      wrap(event(2)),
      { kind: 'reset' },
    ]);
    expect(state.events).toEqual([]);
    expect(state.lastSeq).toBe(0);
    expect(state.connection).toBe('live');
    expect(state.hasBeenLive).toBe(true);
  });

  it('accepts seq 1 again after a reset — a new core counts from 1', () => {
    const state = applyAll([wrap(event(40)), { kind: 'reset' }, wrap(event(1))]);
    expect(state.events.map((e) => e.seq)).toEqual([1]);
  });

  it('tracks whether the stream has ever been live', () => {
    const connecting = applyAll([{ kind: 'connection', state: 'connecting' }]);
    expect(connecting.hasBeenLive).toBe(false);
    const dropped = applyAll(
      [
        { kind: 'connection', state: 'live' },
        { kind: 'connection', state: 'down' },
      ],
      connecting,
    );
    expect(dropped).toMatchObject({ connection: 'down', hasBeenLive: true });
  });

  it(`keeps at most ${String(MAX_EVENTS)} events`, () => {
    const messages = Array.from({ length: MAX_EVENTS + 5 }, (_, i) => wrap(event(i + 1)));
    const state = applyAll(messages);
    expect(state.events).toHaveLength(MAX_EVENTS);
    expect(state.events[0]?.seq).toBe(6);
    expect(state.lastSeq).toBe(MAX_EVENTS + 5);
  });
});

describe('connectStream', () => {
  beforeEach(() => {
    useStreamStore.setState(INITIAL_STREAM);
  });

  it('feeds every bridge message into the store, and unsubscribes', () => {
    let listener: ((message: CoreStreamMessage) => void) | null = null;
    const unsubscribe = vi.fn();
    const bridge = {
      core: {
        request: vi.fn(),
        subscribe: vi.fn((next: (message: CoreStreamMessage) => void) => {
          listener = next;
          return unsubscribe;
        }),
      },
    } as unknown as Pick<AegisBridge, 'core'>;

    const stop = connectStream(bridge);
    const deliver = listener as ((message: CoreStreamMessage) => void) | null;
    deliver?.({ kind: 'connection', state: 'live' });
    deliver?.(wrap(event(1)));

    expect(useStreamStore.getState()).toMatchObject({ connection: 'live', lastSeq: 1 });
    stop();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });
});

describe('engineStatusView', () => {
  it('is idle with no note while live', () => {
    expect(engineStatusView('live', true)).toEqual({
      tone: 'idle',
      label: 'Status: idle',
      note: null,
    });
  });

  it('says it is starting before the first live connection', () => {
    expect(engineStatusView(null, false).note).toBe('Starting the engine…');
    expect(engineStatusView('connecting', false).note).toBe('Starting the engine…');
  });

  it('says reconnecting once it has been live', () => {
    expect(engineStatusView('down', true).note).toBe('Reconnecting…');
    expect(engineStatusView('connecting', true).note).toBe('Reconnecting…');
  });

  it('turns red when the supervisor gives up', () => {
    expect(engineStatusView('unavailable', true)).toMatchObject({
      tone: 'error',
      note: 'The engine is not running.',
    });
  });
});
