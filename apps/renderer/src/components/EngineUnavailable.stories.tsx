import type { Meta, StoryObj } from '@storybook/react-vite';
import type { AegisStoryParameters } from '../../.storybook/states';
import { INITIAL_RECOVERY, useRecoveryStore, type RecoverySnapshot } from '@/stores/recovery';
import { EngineUnavailable } from './EngineUnavailable';

/** Every state of this screen is a recovery-store snapshot, so no story fakes one. */
function recoveryState(state: Partial<RecoverySnapshot>): () => void {
  return () => {
    useRecoveryStore.setState({ ...INITIAL_RECOVERY, ...state });
  };
}

const aegis: AegisStoryParameters = {
  notApplicable: {
    Empty:
      'The screen exists only because the engine is gone; there is no version of it with nothing in it.',
  },
};

const meta = {
  title: 'Recovery/EngineUnavailable',
  component: EngineUnavailable,
  parameters: { aegis },
} satisfies Meta<typeof EngineUnavailable>;

export default meta;

type Story = StoryObj<typeof meta>;

/** `RECOVERY.md § 4` as drawn: the three actions, idle. */
export const Default: Story = {
  beforeEach: recoveryState({}),
};

/** `Restart engine` is waiting on the supervisor, so every button is disabled. */
export const Loading: Story = {
  beforeEach: recoveryState({ busy: 'restart' }),
};

/** The restart came back without a core. */
export const Error: Story = {
  beforeEach: recoveryState({
    notice: {
      tone: 'error',
      text: 'The engine did not start. Try again, or copy a report and send it with your bug report.',
    },
  }),
};

/** The longest notice this screen can show, in the narrowest window (880px). */
export const LongestContent: Story = {
  decorators: [
    (Story) => (
      <div style={{ width: 880 - 64 - 360, height: 600 }}>
        <Story />
      </div>
    ),
  ],
  beforeEach: recoveryState({
    notice: {
      tone: 'error',
      text: 'Aegis cannot reach the part of itself that starts the engine. Close Aegis and open it again.',
    },
  }),
};

/** The one action that leaves no trace of itself on screen says so. */
export const ReportCopied: Story = {
  beforeEach: recoveryState({
    notice: { tone: 'ok', text: 'Report copied. Paste it wherever you are asking for help.' },
  }),
};
