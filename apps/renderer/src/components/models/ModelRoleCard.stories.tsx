import type { Meta, StoryObj } from '@storybook/react-vite';
import type { ModelCatalog, RoleRouteSpec } from '@aegis/shared';
import { CATALOG, route } from '../../../.storybook/models-fixtures';
import { ROLES } from '@/lib/model-roles';
import { ModelRoleCard } from './ModelRoleCard';

const PLANNER = ROLES[0];

const CHAIN: RoleRouteSpec = {
  primary: route('openai', 'gpt-4o').primary,
  fallbacks: [
    {
      provider_id: 'anthropic',
      model: 'claude-sonnet-4-5',
      temperature: 0.2,
      max_output_tokens: 4096,
    },
    { provider_id: 'ollama', model: 'llama3.1:8b', temperature: null, max_output_tokens: null },
  ],
};

/** A catalogue that no longer lists the model the settings name. */
const WITHOUT_GPT4O: ModelCatalog = {
  providers: CATALOG.providers.map((provider) =>
    provider.provider_id === 'openai'
      ? { ...provider, models: provider.models.filter((model) => model.id !== 'gpt-4o') }
      : provider,
  ),
};

const meta = {
  title: 'Models/ModelRoleCard',
  component: ModelRoleCard,
  args: {
    role: PLANNER,
    catalog: CATALOG,
    route: route('openai', 'gpt-4o'),
    onChange: () => undefined,
  },
} satisfies Meta<typeof ModelRoleCard>;

export default meta;

type Story = StoryObj<typeof meta>;

/** One model mapped, no fallbacks: what most users will have. */
export const Default: Story = {};

/** The settings are being saved, so every control is disabled. */
export const Loading: Story = {
  args: { disabled: true },
};

/** First run: this role has no model yet, and the card says so. */
export const Empty: Story = {
  args: { route: null },
};

/**
 * The saved model is not in the catalogue any more — a retired model, or a
 * document written by an older build. The card keeps the value, falls back to a
 * text field and says what is wrong, rather than rendering a blank dropdown and
 * losing the setting.
 */
export const Error: Story = {
  args: { catalog: WITHOUT_GPT4O },
};

/** A full chain (`primary → secondary → local`) with params set on every link. */
export const FullChain: Story = {
  args: { route: CHAIN },
};

/** The longest ids and labels this can hold, in the narrowest panel (880px). */
export const LongestContent: Story = {
  args: {
    route: {
      primary: {
        provider_id: 'openrouter',
        model: 'meta-llama/llama-3.1-405b-instruct:free-with-a-very-long-suffix',
        temperature: 1.75,
        max_output_tokens: 128000,
      },
      fallbacks: CHAIN.fallbacks,
    },
  },
  decorators: [
    (Story) => (
      <div style={{ width: 880 - 64 - 360, padding: 8 }}>
        <Story />
      </div>
    ),
  ],
};
