import type { AegisBridge, Unsubscribe } from '@aegis/shared';
import { useStreamStore, type StreamStore } from '@/stores/stream';

/**
 * Feeds the stream store from `window.aegis.core.subscribe`.
 *
 * Called once, before the first render: MAIN restarts the stream when the page
 * finishes loading, and a listener registered after that would miss the reset
 * and the replay that follow it.
 */
export function connectStream(
  bridge: Pick<AegisBridge, 'core'> = window.aegis,
  store: { getState: () => Pick<StreamStore, 'apply'> } = useStreamStore,
): Unsubscribe {
  return bridge.core.subscribe((message) => {
    store.getState().apply(message);
  });
}
