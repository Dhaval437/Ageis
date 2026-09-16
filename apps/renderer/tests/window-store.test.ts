import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AegisBridge, Unsubscribe } from '@aegis/shared';
import { connectWindowState } from '@/lib/window-client';
import { INITIAL_WINDOW, useWindowStore } from '@/stores/window';

/**
 * The window store is pushed, never polled (invariant 15): its only writer is
 * `connectWindowState`, fed by `window.aegis.window.onMaximizedChange`.
 */

/** A stand-in for the preload's push channel, so a test can fire one. */
function bridge(): {
  bridge: Pick<AegisBridge, 'window'>;
  push: (maximized: boolean) => void;
  unsubscribe: () => void;
} {
  const listeners = new Set<(maximized: boolean) => void>();
  const unsubscribe = vi.fn();
  return {
    bridge: {
      window: {
        minimize: vi.fn(),
        maximize: vi.fn(),
        close: vi.fn(),
        onMaximizedChange: (listener): Unsubscribe => {
          listeners.add(listener);
          return unsubscribe;
        },
        setOverlay: vi.fn(),
      },
    },
    push: (maximized) => {
      for (const listener of listeners) listener(maximized);
    },
    unsubscribe,
  };
}

describe('the window store', () => {
  beforeEach(() => {
    useWindowStore.setState(INITIAL_WINDOW);
  });

  it('starts restored, because that is how MAIN opens the window', () => {
    expect(useWindowStore.getState().maximized).toBe(false);
  });

  it('applies what MAIN pushes, in both directions', () => {
    const { bridge: stub, push } = bridge();
    connectWindowState(stub);

    push(true);
    expect(useWindowStore.getState().maximized).toBe(true);

    push(false);
    expect(useWindowStore.getState().maximized).toBe(false);
  });

  it('hands back the bridge unsubscribe, so a reload leaves no listener behind', () => {
    const { bridge: stub, unsubscribe } = bridge();
    connectWindowState(stub)();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it('never writes the store except from a push', () => {
    const { bridge: stub } = bridge();
    connectWindowState(stub);
    // Nothing was pushed, so nothing changed — no initial fetch, no polling.
    expect(useWindowStore.getState().maximized).toBe(false);
  });
});
