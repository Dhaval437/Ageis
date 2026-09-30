import { describe, expect, it } from 'vitest';
import { nativeForeground } from '../src/main/win-input.js';

/**
 * The two Win32 calls the approval window uses to hand the focus back (P3-19), made
 * for real. Nothing here moves the focus: it asks who has it, and asks Windows to
 * bring forward the window that is already in front and one that does not exist.
 */

describe.runIf(process.platform === 'win32')('the real foreground window', () => {
  it('can be asked for, and is a handle or nothing', () => {
    const foreground = nativeForeground();
    expect(foreground).not.toBeNull();
    const handle = foreground?.current() ?? null;
    if (handle !== null) expect(Number(handle)).toBeGreaterThan(0);
  });

  it('answers without throwing for the window already in front', () => {
    const foreground = nativeForeground();
    const handle = foreground?.current() ?? null;
    if (foreground === null || handle === null) return;
    expect(typeof foreground.restore(handle)).toBe('boolean');
  });

  it('says so when Windows refuses: a handle that is no window', () => {
    expect(nativeForeground()?.restore(0x7ffffff0)).toBe(false);
  });
});
