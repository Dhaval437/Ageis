import type { ReactElement } from 'react';

/**
 * Renderer root.
 *
 * The UI is a pure function of the event stream (REMEMBER.md invariant 15) —
 * no polling for task state. The stream client and Zustand store arrive in
 * P0-09; this is a placeholder shell.
 */
export function App(): ReactElement {
  return (
    <main className="flex h-full items-center justify-center">
      <h1 className="text-sm tracking-wide text-neutral-500">Aegis</h1>
    </main>
  );
}
