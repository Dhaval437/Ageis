import type { Meta, StoryObj } from '@storybook/react-vite';
import {
  CATALOG,
  CONFIGURED,
  LIMITS,
  SPEND,
  UNCONFIGURED,
} from '../../../.storybook/models-fixtures';
import { INITIAL_MODELS, useModelsStore, type ModelsSnapshot } from '@/stores/models';
import { ModelsScreen } from './ModelsScreen';

/**
 * Every state of this screen is a store snapshot, so no story fakes one and no
 * story needs a bridge — the same rule `EngineUnavailable.stories.tsx` follows.
 */
function modelsState(state: Partial<ModelsSnapshot>): () => void {
  return () => {
    useModelsStore.setState({ ...INITIAL_MODELS, ...state });
  };
}

const READY: Partial<ModelsSnapshot> = {
  status: 'ready',
  catalog: CATALOG,
  saved: CONFIGURED,
  draft: CONFIGURED,
  spend: SPEND,
};

const meta = {
  title: 'Models/ModelsScreen',
  component: ModelsScreen,
} satisfies Meta<typeof ModelsScreen>;

export default meta;

type Story = StoryObj<typeof meta>;

/** Every role mapped, a key saved, spend well inside the ceiling. */
export const Default: Story = {
  beforeEach: modelsState(READY),
};

/** The one load, before the core has answered. Skeletons, never a spinner. */
export const Loading: Story = {
  beforeEach: modelsState({ status: 'loading' }),
};

/** First run: nothing mapped, no keys, and the screen says what is missing. */
export const Empty: Story = {
  beforeEach: modelsState({
    ...READY,
    saved: UNCONFIGURED,
    draft: UNCONFIGURED,
    spend: { day: { cents: 0, tokens: 0, unpriced_calls: 0 }, limits: LIMITS },
  }),
};

/** The core could not be reached at all, so there is nothing to show but why. */
export const Error: Story = {
  beforeEach: modelsState({
    status: 'error',
    error: 'Aegis cannot reach the engine. Check the engine status in the title bar.',
  }),
};

/** A save the core refused, with the one sentence it wrote for the user. */
export const SaveRefused: Story = {
  beforeEach: modelsState({
    ...READY,
    draft: { ...CONFIGURED, custom_base_url: 'http://example.com/v1' },
    notice: {
      tone: 'error',
      text: 'Use https:// for an address that is not on this machine.',
    },
  }),
};

/** Unsaved edits: *Save changes* is live and the screen says there are some. */
export const Unsaved: Story = {
  beforeEach: modelsState({
    ...READY,
    draft: { ...CONFIGURED, limits: { ...LIMITS, day_cents: 250 } },
  }),
};

/** The longest ids, the largest numbers, in the narrowest panel (880px). */
export const LongestContent: Story = {
  decorators: [
    (Story) => (
      <div style={{ width: 880 - 64 - 360, height: 600 }}>
        <Story />
      </div>
    ),
  ],
  beforeEach: modelsState({
    ...READY,
    saved: {
      ...CONFIGURED,
      custom_base_url: 'https://a-very-long-internal-gateway-hostname.example.internal:8443/v1',
    },
    draft: {
      ...CONFIGURED,
      custom_base_url: 'https://a-very-long-internal-gateway-hostname.example.internal:8443/v1',
    },
    spend: {
      day: { cents: 987_654.321, tokens: 987_654_321, unpriced_calls: 1234 },
      limits: { ...LIMITS, day_cents: 1_000_000 },
    },
  }),
};
