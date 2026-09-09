import type { ReactElement } from 'react';
import { AppShell } from '@/components/AppShell';

/**
 * Renderer root.
 *
 * The UI is a pure function of the event stream (REMEMBER.md invariant 15) —
 * no polling for task state. The stream client and Zustand store arrive in
 * P0-09; the shell it feeds is P0-03.
 */
export function App(): ReactElement {
  return <AppShell />;
}
