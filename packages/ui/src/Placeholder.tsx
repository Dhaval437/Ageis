import type { ReactElement } from 'react';

/** Placeholder component. Real shared components arrive with P0-03. */
export function Placeholder({ label }: { label: string }): ReactElement {
  return <span data-testid="aegis-ui-placeholder">{label}</span>;
}
