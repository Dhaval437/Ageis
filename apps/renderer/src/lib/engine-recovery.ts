import type { BridgeErrorCode, BridgeResult } from '@aegis/shared';

/**
 * What the Engine-unavailable screen says after each of its three actions
 * (`RECOVERY.md § 4`).
 *
 * Pure and separate from the component so every outcome has a test, and so the
 * copy sits in one place to be checked against `UI.md § 11`: what happened, then
 * what to do about it. The bridge's own error text is deliberately **not**
 * shown — it is written for a developer, and from P2 onward the engine's
 * failures can quote text the agent scraped off the user's screen.
 */

export type RecoveryAction = 'restart' | 'logs' | 'report';

export interface RecoveryNotice {
  readonly tone: 'ok' | 'error';
  readonly text: string;
}

const FAILED: Record<RecoveryAction, string> = {
  restart:
    'The engine did not start. Try again, or copy a report and send it with your bug report.',
  logs: 'Aegis could not open the log folder. Copy a report instead.',
  report: 'The report could not be copied. Try once more.',
};

const NOT_RUNNING =
  'Aegis cannot reach the part of itself that starts the engine. Close Aegis and open it again.';

const SUCCEEDED: Partial<Record<RecoveryAction, string>> = {
  // A restart that worked replaces this whole screen, and an opened folder is
  // its own confirmation; only the clipboard leaves no trace of itself.
  report: 'Report copied. Paste it wherever you are asking for help.',
};

function failure(action: RecoveryAction, code: BridgeErrorCode): RecoveryNotice {
  return { tone: 'error', text: code === 'unavailable' ? NOT_RUNNING : FAILED[action] };
}

/** `null` means "say nothing": the result speaks for itself on screen. */
export function recoveryNotice(
  action: RecoveryAction,
  result: BridgeResult<unknown>,
): RecoveryNotice | null {
  if (!result.ok) return failure(action, result.error.code);
  const text = SUCCEEDED[action];
  return text === undefined ? null : { tone: 'ok', text };
}
