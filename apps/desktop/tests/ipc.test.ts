import { describe, expect, it } from 'vitest';
import { isSenderTrusted } from '../src/main/ipc.js';

/**
 * `ipc.ts` is the one place in MAIN that touches Electron, so only its pure
 * part is unit-testable. That part is the security-relevant one: which senders
 * are allowed to reach a bridge handler at all.
 */
describe('isSenderTrusted', () => {
  it('trusts the window MAIN opened', () => {
    expect(isSenderTrusted(7, 7)).toBe(true);
  });

  it('refuses any other sender', () => {
    expect(isSenderTrusted(8, 7)).toBe(false);
  });

  it('refuses everything when there is no window to trust', () => {
    // `trustedId` is null with no window, or with a destroyed one — the state
    // between `close()` and the app quitting, when handlers still exist.
    expect(isSenderTrusted(7, null)).toBe(false);
    expect(isSenderTrusted(0, null)).toBe(false);
  });
});
