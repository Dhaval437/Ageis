import type { Meta, StoryObj } from '@storybook/react-vite';
import type { KillSwitchStop } from '@aegis/shared';
import {
  INITIAL_KILL_SWITCH,
  useKillSwitchStore,
  type KillSwitchArmed,
} from '@/stores/kill-switch';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { KillSwitchBar } from './KillSwitchBar';

const UNARMED: KillSwitchArmed = {
  kind: 'unarmed',
  message: 'The kill switch is not armed: another program is using Ctrl+Alt+Shift+Q.',
};

function stop(outcome: KillSwitchStop['outcome'], elapsedMs: number): KillSwitchStop {
  return { at: new Date().toISOString(), outcome, elapsedMs };
}

/** Every story is a stream stop plus what MAIN said about the shortcut. */
function state(armed: KillSwitchArmed, last: KillSwitchStop | null): () => void {
  return () => {
    useStreamStore.setState({ ...INITIAL_STREAM, connection: 'live', stop: last });
    useKillSwitchStore.setState({ ...INITIAL_KILL_SWITCH, armed });
  };
}

const meta: Meta<typeof KillSwitchBar> = {
  title: 'Main/KillSwitchBar',
  component: KillSwitchBar,
  decorators: [
    (Story) => (
      // The conversation panel's width at the default window size.
      <div style={{ width: 676 }}>
        <Story />
      </div>
    ),
  ],
  parameters: {
    aegis: {
      notApplicable: {
        Loading:
          'MAIN is asked once whether the shortcut is armed, and until it answers the bar says nothing: a "checking the kill switch" line on every launch would be noise.',
      },
    },
  },
};

export default meta;

type Story = StoryObj<typeof meta>;

/** `UI.md § 7`: the person pressed the kill switch and the engine answered. */
export const Default: Story = {
  beforeEach: state({ kind: 'armed' }, stop('acknowledged', 4)),
};

/** Armed and nothing stopped: the bar is not there at all. */
export const Empty: Story = {
  beforeEach: state({ kind: 'armed' }, null),
};

/** Another program owns the shortcut, so pressing it would do nothing. */
export const Error: Story = {
  beforeEach: state(UNARMED, null),
};

/** A hung engine that had to be ended. */
export const Terminated: Story = {
  beforeEach: state({ kind: 'armed' }, stop('terminated', 102)),
};

/** Both at once: an unarmed shortcut, and a stop made from the tray. */
export const LongestContent: Story = {
  beforeEach: state(UNARMED, stop('terminated', 1_234)),
};
