import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type * as KoffiModule from 'koffi';
import { MODIFIER_VKS, describeRelease, releaseModifiers } from '../src/main/modifier-release.js';
import { AEGIS_SIGNATURE, nativeKeyInput, type KeyInput } from '../src/main/win-input.js';

/**
 * MAIN's own modifier release (`RECOVERY.md § 4`, P3-15): which keys, that it never
 * stops part-way, and — live, on this machine — that a held Shift and right Ctrl
 * really come up.
 */

type Koffi = typeof KoffiModule;

const LSHIFT = 0xa0;
const KEYEVENTF_EXTENDEDKEY = 0x0001;
const KEYEVENTF_KEYUP = 0x0002;
const RCONTROL = 0xa3;

function fakeKeyboard(down: number[], failing: number[] = []): KeyInput & { ups: number[] } {
  const held = new Set(down);
  const ups: number[] = [];
  return {
    ups,
    isDown: (vk) => held.has(vk),
    keyUp: (vk) => {
      if (failing.includes(vk)) throw new Error('refused');
      held.delete(vk);
      ups.push(vk);
    },
  };
}

describe('releaseModifiers', () => {
  it('covers Shift, Ctrl, Alt and Windows, both sides', () => {
    expect([...MODIFIER_VKS].sort()).toEqual(
      [0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0x5b, 0x5c].sort(),
    );
  });

  it('releases exactly the modifiers that are down', () => {
    const keyboard = fakeKeyboard([LSHIFT, RCONTROL, 0x41]);
    const report = releaseModifiers(keyboard);
    expect([...report.released].sort()).toEqual([LSHIFT, RCONTROL].sort());
    expect(keyboard.ups.sort()).toEqual([LSHIFT, RCONTROL].sort());
    expect(keyboard.ups).not.toContain(0x41); // not a modifier: not ours to touch
  });

  it('never stops part-way: one refused key does not strand the others', () => {
    const keyboard = fakeKeyboard(MODIFIER_VKS.slice(), [0xa0]);
    const report = releaseModifiers(keyboard);
    expect(report.failed).toEqual([0xa0]);
    expect(report.released).toHaveLength(MODIFIER_VKS.length - 1);
  });

  it('a key whose state cannot even be read counts as failed, not as up', () => {
    const report = releaseModifiers({
      isDown: (vk) => {
        if (vk === RCONTROL) throw new Error('no');
        return false;
      },
      keyUp: () => undefined,
    });
    expect(report.failed).toEqual([RCONTROL]);
  });

  it('says so when there is no keyboard at all', () => {
    const report = releaseModifiers(null);
    expect(report.unavailable).toBe(true);
    expect(describeRelease('x', report)).toMatch(/no native keyboard/);
  });

  it('logs key codes and counts, and shouts when a key may still be held', () => {
    expect(describeRelease('died', { released: [0xa0], failed: [], unavailable: false })).toBe(
      '[main] died: released 1 (0xa0)',
    );
    expect(describeRelease('died', { released: [], failed: [0xa3], unavailable: false })).toMatch(
      /FAILED 0xa3 — a key may still be held/,
    );
  });
});

describe('the signature', () => {
  it('is the same value the core stamps on its own events', () => {
    const source = readFileSync(
      fileURLToPath(new URL('../../../core/aegis_core/actuation/signature.py', import.meta.url)),
      'utf8',
    );
    const match = /AEGIS_SIGNATURE: Final = (0x[0-9A-Fa-f]+)/.exec(source);
    expect(match?.[1]).toBeDefined();
    expect(AEGIS_SIGNATURE).toBe(Number(match?.[1]));
  });
});

describe.runIf(process.platform === 'win32')('live, on this machine’s keyboard', () => {
  /** A tagged KEYDOWN, sent the same way `win-input.ts` sends its KEYUP. Bound once:
   * koffi type names are process-wide. */
  const sendKey = (() => {
    const koffi = createRequire(import.meta.url)('koffi') as Koffi;
    const user32 = koffi.load('user32.dll');
    const KI = koffi.struct('TEST_KEYBDINPUT', {
      wVk: 'uint16',
      wScan: 'uint16',
      dwFlags: 'uint32',
      time: 'uint32',
      dwExtraInfo: 'uintptr_t',
    });
    const MI = koffi.struct('TEST_MOUSEINPUT', {
      dx: 'int32',
      dy: 'int32',
      mouseData: 'uint32',
      dwFlags: 'uint32',
      time: 'uint32',
      dwExtraInfo: 'uintptr_t',
    });
    const INPUT = koffi.struct('TEST_INPUT', {
      type: 'uint32',
      u: koffi.union('TEST_INPUT_UNION', { mi: MI, ki: KI }),
    });
    const send = user32.func(
      'uint32 __stdcall SendInput(uint32 cInputs, const TEST_INPUT *pInputs, int cbSize)',
    );
    return (vk: number, flags: number): number =>
      Number(
        send(
          1,
          [
            {
              type: 1,
              u: {
                ki: { wVk: vk, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: AEGIS_SIGNATURE },
              },
            },
          ],
          koffi.sizeof(INPUT),
        ),
      );
  })();

  function keyDown(vk: number): void {
    expect(sendKey(vk, vk === RCONTROL ? 1 : 0)).toBe(1);
  }

  it('a Shift and a right Ctrl left down by "a dead core" come up', () => {
    const keyboard = nativeKeyInput();
    expect(keyboard).not.toBeNull();
    if (keyboard === null) return;
    try {
      keyDown(LSHIFT);
      keyDown(RCONTROL);
      expect(keyboard.isDown(LSHIFT)).toBe(true);
      expect(keyboard.isDown(RCONTROL)).toBe(true);

      const started = performance.now();
      const report = releaseModifiers(keyboard);
      const elapsedMs = performance.now() - started;

      expect(report.released).toEqual(expect.arrayContaining([LSHIFT, RCONTROL]));
      expect(report.failed).toEqual([]);
      expect(keyboard.isDown(LSHIFT)).toBe(false);
      expect(keyboard.isDown(RCONTROL)).toBe(false);
      // Inside the kill switch's 200 ms budget with room to spare.
      expect(elapsedMs).toBeLessThan(20);
    } finally {
      // Whatever happened above, nothing this test pressed stays down — and the
      // cleanup goes through the test's own SendInput, not the code under test: a
      // broken release must not leave the developer's Shift and Ctrl held.
      sendKey(LSHIFT, KEYEVENTF_KEYUP);
      sendKey(RCONTROL, KEYEVENTF_KEYUP | KEYEVENTF_EXTENDEDKEY);
    }
  });
});
