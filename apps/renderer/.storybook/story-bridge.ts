import type { AegisBridge, BridgeResult } from '@aegis/shared';

/**
 * `window.aegis` for stories. There is no preload in Storybook, and a component
 * that calls the bridge would otherwise throw. Every call answers the honest
 * "nothing is behind this" shape the real bridge uses for an unbuilt subsystem,
 * so a story can never reach a core, a file or the OS.
 *
 * Stories show state through the stores, never through this bridge.
 */
function unavailable<T>(): Promise<BridgeResult<T>> {
  return Promise.resolve({
    ok: false,
    error: { code: 'unavailable', message: 'Storybook has no engine.' },
  });
}

const noop = (): void => undefined;

export const storyBridge: AegisBridge = {
  core: {
    request: unavailable,
    subscribe: () => noop,
    restart: unavailable,
  },
  window: {
    minimize: noop,
    maximize: noop,
    close: noop,
    // Stories set the maximised state through the window store, as they do
    // every other state, so the bridge never pushes one.
    onMaximizedChange: () => noop,
    setOverlay: unavailable,
  },
  hotkeys: {
    get: unavailable,
    set: unavailable,
  },
  system: {
    pickFolder: unavailable,
    openPath: unavailable,
    revealInExplorer: unavailable,
  },
  updates: {
    check: unavailable,
    install: unavailable,
    onStatus: () => noop,
  },
  app: {
    version: () => Promise.resolve('0.0.0-storybook'),
    logsPath: () => Promise.resolve(''),
    copyDiagnosticReport: unavailable,
    onDeepLink: () => noop,
  },
};

/** `Window.aegis` is readonly in the app's types, because only the preload sets it. */
export function installStoryBridge(): void {
  Object.defineProperty(window, 'aegis', { value: storyBridge, configurable: true });
}
