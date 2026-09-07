/**
 * Where MAIN loads the renderer from.
 *
 * Kept free of any `electron` import so it stays a plain, testable function —
 * everything it needs is passed in.
 */

import { join, resolve } from 'node:path';

export interface RendererEntryInput {
  /** `app.isPackaged`. */
  readonly isPackaged: boolean;
  /** Vite dev server URL, when a dev session is running. */
  readonly devServerUrl: string | undefined;
  /** `app.getAppPath()` — `apps/desktop` unpackaged, the asar root once packaged. */
  readonly appPath: string;
}

export type RendererEntry =
  | { readonly kind: 'url'; readonly value: string }
  | { readonly kind: 'file'; readonly value: string };

/**
 * A packaged build never trusts a dev server URL, even if one is in the
 * environment: a released app that can be pointed at an arbitrary origin by an
 * env var is a remote-code-execution hole, not a convenience.
 *
 * P7-02 must copy `apps/renderer/dist` to `renderer/` inside the app bundle for
 * the packaged branch to resolve.
 */
export function resolveRendererEntry(input: RendererEntryInput): RendererEntry {
  if (!input.isPackaged && input.devServerUrl !== undefined && input.devServerUrl !== '') {
    return { kind: 'url', value: input.devServerUrl };
  }

  if (input.isPackaged) {
    return { kind: 'file', value: join(input.appPath, 'renderer', 'index.html') };
  }

  // Unpackaged with no dev server: load the renderer's own `vite build` output.
  return { kind: 'file', value: resolve(input.appPath, '..', 'renderer', 'dist', 'index.html') };
}
