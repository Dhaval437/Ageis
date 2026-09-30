import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CoreStreamMessage } from '@aegis/shared';
import { connectApprovalStream } from '@/lib/approval-client';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';

/**
 * The approval window's stream (P3-19): its page is its own JavaScript context, so its
 * stream store is fed from `window.aegisApproval`, and from nothing else.
 */

beforeEach(() => {
  useStreamStore.setState(INITIAL_STREAM);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('connectApprovalStream', () => {
  it('feeds the stream store from the approval bridge, until unsubscribed', () => {
    let listener: ((message: CoreStreamMessage) => void) | null = null;
    const unsubscribe = vi.fn();
    const subscribe = vi.fn((next: (message: CoreStreamMessage) => void) => {
      listener = next;
      return unsubscribe;
    });
    vi.stubGlobal('aegisApproval', { subscribe, answer: vi.fn() });

    const stop = connectApprovalStream();
    expect(subscribe).toHaveBeenCalledOnce();
    (listener as ((message: CoreStreamMessage) => void) | null)?.({
      kind: 'connection',
      state: 'live',
    });
    expect(useStreamStore.getState().connection).toBe('live');

    stop();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it('never listens on the main window’s bridge', () => {
    const subscribe = vi.fn();
    vi.stubGlobal('aegis', { core: { subscribe } });
    vi.stubGlobal('aegisApproval', undefined);
    connectApprovalStream()();
    expect(subscribe).not.toHaveBeenCalled();
  });

  it('does nothing, and does not throw, without its bridge', () => {
    vi.stubGlobal('aegisApproval', undefined);
    expect(() => {
      connectApprovalStream()();
    }).not.toThrow();
    expect(useStreamStore.getState()).toMatchObject(INITIAL_STREAM);
  });
});
