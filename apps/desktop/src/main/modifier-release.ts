/**
 * MAIN's release of held modifier keys (`RECOVERY.md § 4`, P3-15).
 *
 * > If the core dies while a task is running, MAIN's very first action — before any
 * > UI update — is to release all modifier keys via its own input path.
 *
 * The core tracks exactly which keys *it* holds and releases only those (the
 * 2026-09-09 Decision Log row), so it never lifts a key the human is pressing. MAIN
 * has no such record: the core that had it is gone. So MAIN asks Windows which of the
 * eight modifiers are down and sends a `KEYUP` for each. That can include a key the
 * person is physically holding — most obviously the kill switch's own `Ctrl+Alt+Shift`
 * — and the cost is only that the key reads as up until they let go and press it
 * again. The alternative, a `Ctrl` a dead agent holds down for good, is the P0.
 *
 * `electron`-free: the keyboard is injected (`win-input.ts` in production).
 */

import type { KeyInput } from './win-input.js';

/** Shift, Ctrl, Alt and Windows, both sides — the keys invariant 3 is about. */
export const MODIFIER_VKS: readonly number[] = [
  0xa0, // left Shift
  0xa1, // right Shift
  0xa2, // left Ctrl
  0xa3, // right Ctrl
  0xa4, // left Alt
  0xa5, // right Alt
  0x5b, // left Windows
  0x5c, // right Windows
];

export interface ReleaseReport {
  /** Keys that were down and were sent a `KEYUP`. */
  readonly released: readonly number[];
  /** Keys whose check or release threw. Non-empty means a key may still be held. */
  readonly failed: readonly number[];
  /** `true` when there was no keyboard to use at all (not Windows, or it failed to load). */
  readonly unavailable: boolean;
}

/**
 * Release every modifier that is down. **Never stops part-way:** a key that fails
 * does not stop the others, as in the core's own `release_all()`.
 */
export function releaseModifiers(input: KeyInput | null): ReleaseReport {
  if (input === null) return { released: [], failed: [], unavailable: true };
  const released: number[] = [];
  const failed: number[] = [];
  for (const vk of MODIFIER_VKS) {
    try {
      if (!input.isDown(vk)) continue;
      input.keyUp(vk);
      released.push(vk);
    } catch {
      failed.push(vk);
    }
  }
  return { released, failed, unavailable: false };
}

/** One log line, with counts and key codes only. */
export function describeRelease(reason: string, report: ReleaseReport): string {
  if (report.unavailable) return `[main] ${reason}: no native keyboard, so no modifiers released`;
  const hex = (vks: readonly number[]): string => vks.map((vk) => `0x${vk.toString(16)}`).join(' ');
  const parts = [`released ${String(report.released.length)}`];
  if (report.released.length > 0) parts.push(`(${hex(report.released)})`);
  if (report.failed.length > 0)
    parts.push(`FAILED ${hex(report.failed)} — a key may still be held`);
  return `[main] ${reason}: ${parts.join(' ')}`;
}
