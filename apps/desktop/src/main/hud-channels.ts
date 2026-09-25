/**
 * The OverlayHUD's IPC channels (P3-13). The HUD preload (`preload/hud.cts`) spells
 * these out itself — a sandboxed preload cannot import this file — and
 * `tests/hud-bridge.test.ts` asserts the two lists are identical.
 */

export const HUD_EVENT_CHANNEL = 'aegis:hud:event';

export const HUD_SEND_CHANNELS = {
  stop: 'aegis:hud:stop',
  showMain: 'aegis:hud:show-main',
} as const;

export const HUD_INVOKE_CHANNELS = {
  deny: 'aegis:hud:deny',
} as const;

/** Every channel the HUD may use, in any direction. */
export const HUD_CHANNELS: readonly string[] = [
  HUD_EVENT_CHANNEL,
  ...Object.values(HUD_SEND_CHANNELS),
  ...Object.values(HUD_INVOKE_CHANNELS),
];

/** An approval id as the core issues them: a positive safe integer, nothing else. */
export function parseApprovalId(input: unknown): number | null {
  return typeof input === 'number' && Number.isSafeInteger(input) && input >= 1 ? input : null;
}
