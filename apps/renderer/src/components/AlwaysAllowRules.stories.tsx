import type { Meta, StoryObj } from '@storybook/react-vite';
import type { AllowRuleInfo } from '@aegis/shared';
import { INITIAL_RULES, useRulesStore, type RulesState } from '@/stores/rules';
import { AlwaysAllowRules } from './AlwaysAllowRules';

const EXACT: AllowRuleInfo = {
  id: 1,
  kind: 'exact',
  tool: 'input.click',
  folder: null,
  task_id: null,
  created_at: '2026-09-25T09:14:03.120Z',
};
const IN_FOLDER: AllowRuleInfo = {
  id: 2,
  kind: 'tool_in_folder',
  tool: 'fs.move',
  folder: 'd:\\work\\invoices',
  task_id: null,
  created_at: '2026-09-26T16:40:51.002Z',
};
const FOR_TASK: AllowRuleInfo = {
  id: 3,
  kind: 'tool_for_task',
  tool: 'fs.write',
  folder: null,
  task_id: 'task-7f3c',
  created_at: '2026-09-28T11:02:18.500Z',
};

/** Every story is a store state; the list has nothing of its own to fake. */
function state(patch: Partial<RulesState>): () => void {
  return () => {
    useRulesStore.setState({ ...INITIAL_RULES, ...patch });
  };
}

const meta: Meta<typeof AlwaysAllowRules> = {
  title: 'Rules/AlwaysAllowRules',
  component: AlwaysAllowRules,
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

/** `UI.md § 8.3`: one rule of each kind, newest first as the core sends them. */
export const Default: Story = {
  beforeEach: state({ status: 'ready', rules: [FOR_TASK, IN_FOLDER, EXACT] }),
};

/** Before the engine has answered. */
export const Loading: Story = {
  beforeEach: state({ status: 'loading' }),
};

/** No rule yet: Aegis asks every time. */
export const Empty: Story = {
  beforeEach: state({ status: 'ready' }),
};

/** The list could not be read. */
export const Error: Story = {
  beforeEach: state({
    status: 'error',
    loadError: 'Aegis could not read your rules. Restart the engine and try again.',
  }),
};

/** A revoke that did not land: the rule still applies, and the list says so. */
export const RevokeFailed: Story = {
  beforeEach: state({
    status: 'ready',
    rules: [IN_FOLDER],
    notice: {
      tone: 'error',
      text: 'Aegis could not revoke that rule. It still applies. Try again.',
    },
  }),
};

/** Long tool names and deep folders, many rules, one revoke in flight. */
export const LongestContent: Story = {
  beforeEach: state({
    status: 'ready',
    revoking: 4,
    rules: Array.from({ length: 12 }, (_, i) => ({
      id: i + 4,
      kind: i % 2 === 0 ? ('tool_in_folder' as const) : ('tool_for_task' as const),
      tool: `browser.fill_form_field_with_value_from_spreadsheet_${String(i)}`,
      folder:
        i % 2 === 0
          ? `\\\\fileserver\\finance\\reporting\\2026\\q3\\regional-submissions-final-reviewed-${String(i)}`
          : null,
      task_id: i % 2 === 0 ? null : `task-2026-09-28-quarterly-close-${String(i)}`,
      created_at: '2026-09-28T11:02:18.500Z',
    })),
  }),
};
