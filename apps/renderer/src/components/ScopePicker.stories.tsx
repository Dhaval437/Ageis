import type { Meta, StoryObj } from '@storybook/react-vite';
import type { ScopeInfo } from '@aegis/shared';
import { INITIAL_SCOPES, useScopesStore, type ScopesState } from '@/stores/scopes';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { ScopePicker } from './ScopePicker';

const SCOPES: ScopeInfo[] = [
  { id: 2, name: 'Photos', folders: ['c:\\users\\bo\\pictures'], apps: [] },
  { id: 1, name: 'Work files', folders: ['d:\\work\\invoices', 'd:\\work\\reports'], apps: [] },
];

function state(patch: Partial<ScopesState>, taskRunning = false): () => void {
  return () => {
    useScopesStore.setState({ ...INITIAL_SCOPES, ...patch });
    useStreamStore.setState({
      ...INITIAL_STREAM,
      connection: 'live',
      events: taskRunning
        ? [
            {
              seq: 1,
              ts: new Date().toISOString(),
              task_id: '7',
              type: 'task.status',
              payload: { status: 'RUNNING' },
            },
          ]
        : [],
      lastSeq: taskRunning ? 1 : 0,
    });
  };
}

const meta: Meta<typeof ScopePicker> = {
  title: 'Main/ScopePicker',
  component: ScopePicker,
  args: { onManage: () => undefined },
  decorators: [
    (Story) => (
      <div style={{ display: 'flex', justifyContent: 'flex-end', width: 400, height: 240 }}>
        <Story />
      </div>
    ),
  ],
};

export default meta;

type Story = StoryObj<typeof meta>;

/** `UI.md § 4.1`: the chosen scope's name, and the list with *Manage scopes…*. */
export const Default: Story = {
  beforeEach: state({ status: 'ready', scopes: SCOPES, selectedId: 1 }),
};

/** The list is still loading. */
export const Loading: Story = {
  beforeEach: state({ status: 'loading' }),
};

/** No scopes: the list says so and offers only *Manage scopes…*. */
export const Empty: Story = {
  beforeEach: state({ status: 'ready' }),
};

/** The scopes could not be read. */
export const Error: Story = {
  beforeEach: state({ status: 'error', loadError: 'Aegis could not read your scopes.' }),
};

/** A task is running, so the scope cannot change — the reason is on hover. */
export const TaskRunning: Story = {
  beforeEach: state({ status: 'ready', scopes: SCOPES, selectedId: 1 }, true),
};

/** A long scope name is cut to the pill's width. */
export const LongestContent: Story = {
  beforeEach: state({
    status: 'ready',
    scopes: [
      {
        id: 9,
        name: 'Quarterly reporting for the EMEA, APAC and Americas regions',
        folders: ['d:\\q3'],
        apps: [],
      },
    ],
    selectedId: 9,
  }),
};
