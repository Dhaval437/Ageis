import type { Meta, StoryObj } from '@storybook/react-vite';
import { INITIAL_RULES, useRulesStore, type RulesState } from '@/stores/rules';
import { RulePreferences } from './RulePreferences';

function state(patch: Partial<RulesState>): () => void {
  return () => useRulesStore.setState({ ...INITIAL_RULES, ...patch });
}

const meta: Meta<typeof RulePreferences> = {
  title: 'Rules/RulePreferences',
  component: RulePreferences,
  decorators: [
    (Story) => (
      <div style={{ width: 676 }}>
        <Story />
      </div>
    ),
  ],
};
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  beforeEach: state({
    status: 'ready',
    alwaysAsk: [
      { tool: 'fs.write', created_at: '2026-10-13T08:00:00.000Z' },
      { tool: 'input.click', created_at: '2026-10-12T08:00:00.000Z' },
    ],
    never: [
      "Aegis never changes Windows' own files.",
      "Aegis never opens a browser's saved passwords, cookies or payment details.",
    ],
  }),
};

export const Loading: Story = { beforeEach: state({ status: 'loading' }) };

export const Empty: Story = { beforeEach: state({ status: 'ready' }) };

export const Error: Story = {
  beforeEach: state({ status: 'error', loadError: 'Aegis could not read your rules.' }),
};

export const LongestContent: Story = {
  beforeEach: state({
    status: 'ready',
    alwaysAsk: Array.from({ length: 12 }, (_, index) => ({
      tool: `browser.fill_form_field_with_value_from_spreadsheet_${String(index)}`,
      created_at: '2026-10-13T08:00:00.000Z',
    })),
    never: Array.from(
      { length: 28 },
      (_, index) =>
        `Aegis never changes a protected system or credential location with a deliberately long description ${String(index)}.`,
    ),
  }),
};
