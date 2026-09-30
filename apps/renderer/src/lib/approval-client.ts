import type { Unsubscribe } from '@aegis/shared';
import { useStreamStore } from '@/stores/stream';

/**
 * The approval window's stream (P3-19). The window is its own JavaScript context, so
 * it has its own stream store, fed from `window.aegisApproval`. Without that bridge
 * (Storybook, the main window) it does nothing.
 */
export function connectApprovalStream(): Unsubscribe {
  const bridge = typeof window === 'undefined' ? undefined : window.aegisApproval;
  if (bridge === undefined) return () => undefined;
  return bridge.subscribe((message) => {
    useStreamStore.getState().apply(message);
  });
}
