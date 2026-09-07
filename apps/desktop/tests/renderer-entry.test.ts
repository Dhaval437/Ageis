import { describe, expect, it } from 'vitest';
import { resolveRendererEntry } from '../src/main/renderer-entry.js';

const APP_PATH = '/repo/apps/desktop';

describe('resolveRendererEntry', () => {
  it('uses the dev server when one is running and the app is not packaged', () => {
    expect(
      resolveRendererEntry({
        isPackaged: false,
        devServerUrl: 'http://localhost:5173',
        appPath: APP_PATH,
      }),
    ).toEqual({ kind: 'url', value: 'http://localhost:5173' });
  });

  it('ignores a dev server URL once packaged', () => {
    const entry = resolveRendererEntry({
      isPackaged: true,
      devServerUrl: 'http://attacker.example',
      appPath: '/install/resources/app.asar',
    });

    expect(entry.kind).toBe('file');
    expect(entry.value).not.toContain('attacker');
  });

  it('falls back to the built renderer when no dev server is set', () => {
    const entry = resolveRendererEntry({
      isPackaged: false,
      devServerUrl: undefined,
      appPath: APP_PATH,
    });

    expect(entry.kind).toBe('file');
    expect(entry.value.replaceAll('\\', '/')).toContain('apps/renderer/dist/index.html');
  });

  it('treats an empty dev server URL as absent', () => {
    expect(
      resolveRendererEntry({ isPackaged: false, devServerUrl: '', appPath: APP_PATH }).kind,
    ).toBe('file');
  });
});
