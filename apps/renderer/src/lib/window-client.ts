import type { AegisBridge, Unsubscribe } from '@aegis/shared';
import { useWindowStore, type WindowStore } from '@/stores/window';

/**
 * Feeds the window store from `window.aegis.window.onMaximizedChange`.
 *
 * Called once, before the first render, for the same reason `connectStream` is:
 * MAIN pushes the current window state when the page finishes loading, and a
 * listener registered after that would wait for the user to resize before the
 * titlebar told the truth.
 */
export function connectWindowState(
  bridge: Pick<AegisBridge, 'window'> = window.aegis,
  store: { getState: () => Pick<WindowStore, 'setMaximized'> } = useWindowStore,
): Unsubscribe {
  return bridge.window.onMaximizedChange((maximized) => {
    store.getState().setMaximized(maximized);
  });
}
