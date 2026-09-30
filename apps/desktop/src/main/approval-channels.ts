/**
 * The approval window's IPC channels (P3-19). Its preload (`preload/approval.cts`)
 * spells these out itself — a sandboxed preload cannot import this file — and
 * `tests/approval-bridge.test.ts` asserts the two lists are identical.
 */

export const APPROVAL_EVENT_CHANNEL = 'aegis:approval:event';

export const APPROVAL_INVOKE_CHANNELS = {
  answer: 'aegis:approval:answer',
} as const;

/** Every channel the approval window may use, in any direction. */
export const APPROVAL_CHANNELS: readonly string[] = [
  APPROVAL_EVENT_CHANNEL,
  ...Object.values(APPROVAL_INVOKE_CHANNELS),
];
