import type { Meta, StoryObj } from '@storybook/react-vite';
import type { ProviderCatalog } from '@aegis/shared';
import { CATALOG } from '../../../.storybook/models-fixtures';
import { ProviderCard } from './ProviderCard';

/** `TypeError`, not `Error`: this file exports a story called `Error`, which shadows it. */
function card(providerId: string): ProviderCatalog {
  const found = CATALOG.providers.find((provider) => provider.provider_id === providerId);
  if (found === undefined) throw new TypeError(`no fixture for ${providerId}`);
  return found;
}

const meta = {
  title: 'Models/ProviderCard',
  component: ProviderCard,
  args: {
    provider: card('openai'),
    baseUrl: '',
    keyDraft: '',
    result: undefined,
    testing: false,
    savingKey: false,
    onKeyDraft: () => undefined,
    onSaveKey: () => undefined,
    onRemoveKey: () => undefined,
    onTest: () => undefined,
    onBaseUrl: () => undefined,
  },
} satisfies Meta<typeof ProviderCard>;

export default meta;

type Story = StoryObj<typeof meta>;

/** A key is saved, shown as `sk-…abcd` and never as itself. */
export const Default: Story = {};

/** The *Test* button is waiting on a real call to the provider. */
export const Loading: Story = {
  args: { testing: true },
};

/** No key yet: the state every provider starts in. */
export const Empty: Story = {
  args: {
    provider: { ...card('openai'), has_key: false, masked_key: null },
  },
};

/** The test came back and the key does not work. The core wrote that sentence. */
export const Error: Story = {
  args: {
    result: {
      provider_id: 'openai',
      valid: false,
      detail: 'The provider rejected that key. Check you copied all of it.',
      latency_ms: 412,
    },
  },
};

/** A working key, confirmed against the provider, with the round trip it took. */
export const Tested: Story = {
  args: {
    result: {
      provider_id: 'openai',
      valid: true,
      detail: 'That key works.',
      latency_ms: 238,
    },
  },
};

/** `UI.md § 8.4`'s local card: no key, and the badge that is the point of it. */
export const Local: Story = {
  args: {
    provider: card('ollama'),
    baseUrl: 'http://127.0.0.1:11434',
    result: {
      provider_id: 'ollama',
      valid: true,
      detail: 'Ollama is running with 3 models installed.',
      latency_ms: 12,
    },
  },
};

/** A gateway with nowhere to point yet, so the address field is what to fill in. */
export const CustomGateway: Story = {
  args: { provider: card('custom'), baseUrl: '' },
};

/** The longest address and message this can hold, in the narrowest panel. */
export const LongestContent: Story = {
  args: {
    provider: card('custom'),
    baseUrl: 'https://a-very-long-internal-gateway-hostname.example.internal:8443/openai/v1',
    result: {
      provider_id: 'custom',
      valid: false,
      detail:
        'Use https:// for an address that is not on this machine. Plain http:// would put your key on the wire in the clear.',
      latency_ms: 30000,
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
