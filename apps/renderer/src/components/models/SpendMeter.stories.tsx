import type { Meta, StoryObj } from '@storybook/react-vite';
import { LIMITS } from '../../../.storybook/models-fixtures';
import { SpendMeter } from './SpendMeter';

const meta = {
  title: 'Models/SpendMeter',
  component: SpendMeter,
  args: { limits: LIMITS },
} satisfies Meta<typeof SpendMeter>;

export default meta;

type Story = StoryObj<typeof meta>;

/** Well inside the ceiling: the ordinary state while a task runs. */
export const Default: Story = {
  args: { day: { cents: 42.5, tokens: 128_400, unpriced_calls: 0 } },
};

/** The first paint, before `GET /v1/models/spend` has answered. */
export const Loading: Story = {
  args: { day: null },
};

/** Nothing spent today. A real number, not a placeholder. */
export const Empty: Story = {
  args: { day: { cents: 0, tokens: 0, unpriced_calls: 0 } },
};

/** Past four fifths of the ceiling: amber, and it says why in words. */
export const NearTheLimit: Story = {
  args: { day: { cents: 880, tokens: 2_400_000, unpriced_calls: 0 } },
};

/**
 * At the ceiling — this component's error state (`UI.md § 9`, *Budget
 * exceeded*). Tasks pause, and the meter says so in words as well as in red.
 */
export const Error: Story = {
  args: { day: { cents: 1000, tokens: 3_100_000, unpriced_calls: 0 } },
};

/** A total missing a price says *at least*, never a short number presented as whole. */
export const IncompleteTotal: Story = {
  args: { day: { cents: 12.75, tokens: 90_000, unpriced_calls: 7 } },
};

/** No ceiling set: the bar is gone, because a bar with no maximum means nothing. */
export const NoLimit: Story = {
  args: {
    day: { cents: 1240.5, tokens: 4_000_000, unpriced_calls: 0 },
    limits: { ...LIMITS, day_cents: null },
  },
};

/** The widest figures this can hold, in a narrow column. */
export const LongestContent: Story = {
  args: {
    day: { cents: 987_654.321, tokens: 987_654_321, unpriced_calls: 1234 },
    limits: { ...LIMITS, day_cents: 1_000_000 },
  },
  decorators: [
    (Story) => (
      <div style={{ width: 280, padding: 8 }}>
        <Story />
      </div>
    ),
  ],
};
