import type { ReactElement } from 'react';
import { AppShell } from '@/components/AppShell';

/**
 * Renderer root.
 *
 * The UI is a pure function of the event stream (REMEMBER.md invariant 15) —
 * no polling for task state. `connectStream` (started in `main.tsx`) feeds
 * `stores/stream.ts`, and components read from that store only.
 */
export function App(): ReactElement {
  return <AppShell />;
}
