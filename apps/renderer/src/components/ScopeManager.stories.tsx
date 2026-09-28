import type { Meta, StoryObj } from '@storybook/react-vite';
import type { ScopeInfo } from '@aegis/shared';
import { INITIAL_SCOPES, useScopesStore, type ScopesState } from '@/stores/scopes';
import { ScopeManager } from './ScopeManager';

const WORK: ScopeInfo = {
  id: 1,
  name: 'Work files',
  folders: ['d:\\work\\invoices', 'd:\\work\\reports'],
  apps: [],
};
const PHOTOS: ScopeInfo = { id: 2, name: 'Photos', folders: ['c:\\users\\bo\\pictures'], apps: [] };

/** Every story is a store state; the manager has nothing of its own to fake. */
function state(patch: Partial<ScopesState>): () => void {
  return () => {
    useScopesStore.setState({ ...INITIAL_SCOPES, ...patch });
  };
}

const meta: Meta<typeof ScopeManager> = {
  title: 'Rules/ScopeManager',
  component: ScopeManager,
  decorators: [
    (Story) => (
      <div style={{ width: 676, height: 560, display: 'flex' }}>
        <Story />
      </div>
    ),
  ],
};

export default meta;

type Story = StoryObj<typeof meta>;

/** `UI.md § 8.3`: two scopes, each with its folders and what it allows. */
export const Default: Story = {
  beforeEach: state({ status: 'ready', scopes: [PHOTOS, WORK], selectedId: 1 }),
};

/** Before the engine has answered. */
export const Loading: Story = {
  beforeEach: state({ status: 'loading' }),
};

/** No scope yet: Aegis can reach no folders. */
export const Empty: Story = {
  beforeEach: state({ status: 'ready' }),
};

/** The list could not be read. */
export const Error: Story = {
  beforeEach: state({
    status: 'error',
    loadError: 'Aegis could not read your scopes. Restart the engine and try again.',
  }),
};

/** A folder the core refused, with its sentence, while the dialog's pick was checked. */
export const Refused: Story = {
  beforeEach: state({
    status: 'ready',
    scopes: [WORK],
    notice: {
      tone: 'error',
      text: 'That folder is one Aegis never touches, so it cannot be in a scope.',
    },
  }),
};

/** A scope at its limits: a long name and deep, long folder paths. */
export const LongestContent: Story = {
  beforeEach: state({
    status: 'ready',
    scopes: [
      {
        id: 3,
        name: 'Quarterly reporting for the EMEA, APAC and Americas regions',
        folders: Array.from(
          { length: 6 },
          (_, i) =>
            `\\\\fileserver\\finance\\reporting\\2026\\q3\\regional-submissions-final-reviewed-${String(i)}`,
        ),
        apps: ['excel.exe', 'outlook.exe'],
      },
    ],
    busy: 'scope:3',
  }),
};
