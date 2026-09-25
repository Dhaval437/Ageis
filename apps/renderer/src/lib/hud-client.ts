import type { AegisHudBridge, Unsubscribe } from '@aegis/shared';
import { useStreamStore } from '@/stores/stream';

/**
 * The OverlayHUD's side of `window.aegisHud` (P3-13). The HUD page has its own
 * JavaScript context, so it has its own stream store, fed from its own preload.
 *
 * Every call tolerates a missing bridge — Storybook has none — by doing nothing:
 * the HUD only ever makes Aegis do less, so a no-op is the safe failure.
 */

function hud(): AegisHudBridge | undefined {
  return typeof window === 'undefined' ? undefined : window.aegisHud;
}

/** Called once, before the first render, so the reset and replay MAIN sends land. */
export function connectHudStream(): Unsubscribe {
  const bridge = hud();
  if (bridge === undefined) return () => undefined;
  return bridge.subscribe((message) => {
    useStreamStore.getState().apply(message);
  });
}

export function stopAgent(): void {
  hud()?.stop();
}

export function showMainWindow(): void {
  hud()?.showMain();
}

/** Deny one approval. Resolves `false` when it could not be sent. */
export async function denyApproval(approvalId: number): Promise<boolean> {
  const bridge = hud();
  if (bridge === undefined) return false;
  try {
    return (await bridge.deny(approvalId)).ok;
  } catch {
    return false;
  }
}
