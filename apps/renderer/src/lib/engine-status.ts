import type { CoreConnection } from '@aegis/shared';

export interface EngineStatusView {
  /** `UI.md § 4.1` status dot: grey idle, red stopped/error. */
  readonly tone: 'idle' | 'error';
  readonly label: string;
  /** Shown beside the name while the engine is not live; `null` when it is. */
  readonly note: string | null;
}

/** Pure, so every connection state has a test. Copy follows `UI.md § 11`. */
export function engineStatusView(
  connection: CoreConnection | null,
  hasBeenLive: boolean,
): EngineStatusView {
  switch (connection) {
    case 'live':
      return { tone: 'idle', label: 'Status: idle', note: null };
    case 'unavailable':
      return {
        tone: 'error',
        label: 'Status: the engine is not running',
        note: 'The engine is not running.',
      };
    default: {
      const note = hasBeenLive ? 'Reconnecting…' : 'Starting the engine…';
      return { tone: 'idle', label: `Status: ${note}`, note };
    }
  }
}
