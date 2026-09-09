import { describe, expect, it } from 'vitest';
import { createPathGrants, isExecutable, isWithin } from '../src/main/path-grants.js';

/**
 * A `realpath` that resolves a fixed set of aliases — a junction, an 8.3 short
 * name, a `..` walk — so the adversarial cases can be exercised without
 * creating them on the developer's disk.
 */
function fakeRealpath(aliases: Record<string, string> = {}) {
  return (path: string): Promise<string> => {
    const resolved = aliases[path.toLowerCase()];
    if (resolved !== undefined) return Promise.resolve(resolved);
    if (path.toLowerCase().includes('missing')) return Promise.reject(new Error('ENOENT'));
    return Promise.resolve(path);
  };
}

const WORK = 'C:\\Users\\Bo\\Work';

describe('isWithin', () => {
  it('accepts the root itself and anything under it', () => {
    expect(isWithin(WORK, WORK)).toBe(true);
    expect(isWithin(WORK, `${WORK}\\notes\\a.txt`)).toBe(true);
  });

  it('is case-insensitive, because Windows filesystems are', () => {
    expect(isWithin(WORK, 'c:\\users\\bo\\WORK\\a.txt')).toBe(true);
  });

  it('refuses a sibling that merely starts with the root name', () => {
    expect(isWithin('C:\\Users\\Bo', 'C:\\Users\\Bob\\secrets.txt')).toBe(false);
    expect(isWithin(WORK, `${WORK}-archive\\a.txt`)).toBe(false);
  });

  it('refuses an empty root rather than matching everything', () => {
    expect(isWithin('', 'C:\\Windows\\System32\\cmd.exe')).toBe(false);
  });
});

describe('isExecutable', () => {
  it('names the things that run when opened', () => {
    for (const path of ['a.exe', 'a.BAT', 'a.ps1', 'a.lnk', 'a.msi', 'a.js', 'a.scr']) {
      expect(isExecutable(`C:\\x\\${path}`), path).toBe(true);
    }
  });

  it('sees through the trailing dots and spaces Win32 strips before opening', () => {
    expect(isExecutable('C:\\x\\payload.exe.')).toBe(true);
    expect(isExecutable('C:\\x\\payload.exe ')).toBe(true);
  });

  it('leaves documents alone', () => {
    for (const path of ['a.txt', 'a.pdf', 'a.docx', 'a.png', 'notes']) {
      expect(isExecutable(`C:\\x\\${path}`), path).toBe(false);
    }
  });
});

describe('createPathGrants', () => {
  it('denies everything until the user grants something', async () => {
    const grants = createPathGrants(fakeRealpath());
    expect(await grants.check(WORK)).toEqual({ ok: false, reason: 'not_granted' });
    expect(grants.roots()).toEqual([]);
  });

  it('allows a granted root and its children', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot(WORK);
    expect(await grants.check(`${WORK}\\notes\\a.txt`)).toEqual({
      ok: true,
      path: `${WORK}\\notes\\a.txt`,
    });
  });

  it('checks the resolved path, not the spelling: a junction out of the root is denied', async () => {
    const grants = createPathGrants(
      fakeRealpath({ 'c:\\users\\bo\\work\\escape': 'C:\\Windows\\System32' }),
    );
    await grants.grantRoot(WORK);
    expect(await grants.check(`${WORK}\\escape`)).toEqual({ ok: false, reason: 'not_granted' });
  });

  it('denies a `..` walk out of the root', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot(WORK);
    expect(await grants.check(`${WORK}\\..\\..\\.ssh\\id_rsa`)).toEqual({
      ok: false,
      reason: 'not_granted',
    });
  });

  it('denies a path that does not exist rather than guessing', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot(WORK);
    expect(await grants.check(`${WORK}\\missing.txt`)).toEqual({
      ok: false,
      reason: 'not_granted',
    });
  });

  it('never grants a root it cannot resolve', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot('C:\\missing\\folder');
    expect(grants.roots()).toEqual([]);
  });

  it('refuses to open an executable even inside a granted root', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot(WORK);
    expect(await grants.checkOpenable(`${WORK}\\setup.exe`)).toEqual({
      ok: false,
      reason: 'executable',
    });
    // The same file is still fine to reveal in Explorer — that shows it, it
    // does not run it.
    expect(await grants.check(`${WORK}\\setup.exe`)).toEqual({
      ok: true,
      path: `${WORK}\\setup.exe`,
    });
  });

  it('does not accumulate duplicate roots', async () => {
    const grants = createPathGrants(fakeRealpath());
    await grants.grantRoot(WORK);
    await grants.grantRoot(WORK);
    expect(grants.roots()).toEqual([WORK]);
  });
});
