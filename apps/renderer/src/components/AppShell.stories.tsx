import type { Decorator, Meta, StoryObj } from '@storybook/react-vite';
import type { AegisStoryParameters } from '../../.storybook/states';
import { INITIAL_STREAM, useStreamStore, type StreamSnapshot } from '@/stores/stream';
import { AppShell } from './AppShell';

/** The window the shell lives in: the default size, or the minimum (`UI.md § 3`). */
function windowOfSize(width: number, height: number): Decorator {
  const WindowFrame: Decorator = (Story) => (
    <div style={{ width, height }}>
      <Story />
    </div>
  );
  return WindowFrame;
}

/** The shell reads only the stream store, so each state is a store snapshot. */
function streamState(state: Partial<StreamSnapshot>): () => void {
  return () => {
    useStreamStore.setState({ ...INITIAL_STREAM, ...state });
  };
}

const aegis: AegisStoryParameters = {
  notApplicable: {
    Empty:
      'Until P4-11 gives the shell a timeline, every section is its empty state, so Default already is one.',
  },
};

const meta = {
  title: 'Shell/AppShell',
  component: AppShell,
  decorators: [windowOfSize(1100, 720)],
  parameters: { aegis },
} satisfies Meta<typeof AppShell>;

export default meta;

type Story = StoryObj<typeof meta>;

/** The engine is live and nothing is running. */
export const Default: Story = {
  beforeEach: streamState({ connection: 'live', hasBeenLive: true }),
};

/** First paint, before MAIN has said anything: "Starting the engine…". */
export const Loading: Story = {
  beforeEach: streamState({ connection: null }),
};

/** The supervisor gave up after three attempts: red dot, "The engine is not running." */
export const Error: Story = {
  beforeEach: streamState({ connection: 'unavailable' }),
};

/** The longest titlebar note, in the smallest window Electron allows (880×600). */
export const LongestContent: Story = {
  decorators: [windowOfSize(880, 600)],
  beforeEach: streamState({ connection: 'unavailable', hasBeenLive: true }),
};

/** The engine was live and dropped: "Reconnecting…". */
export const Reconnecting: Story = {
  beforeEach: streamState({ connection: 'down', hasBeenLive: true }),
};
