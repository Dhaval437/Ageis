import type { Meta, StoryObj } from '@storybook/react-vite';
import type { StreamEvent } from '@aegis/shared';

type Payload = StreamEvent['payload'];
import type { AegisStoryParameters } from '../../.storybook/states';
import { INITIAL_APPROVAL_UI, useApprovalStore, type ApprovalUiSnapshot } from '@/stores/approval';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { ApprovalDialog } from './ApprovalDialog';

/** `UI.md § 5`'s own example: a dangerous deletion. */
const DELETE: Payload = {
  approval_id: 1,
  tool: 'fs.delete',
  prompt: 'Delete 12 files in D:\\work\\invoices\\archive\\ (2024-01.pdf, 2024-02.pdf, … 4.2 MB)',
  why: "The user asked to clear last year's archive after the merge completed.",
  tier: 'DANGEROUS',
  reason: 'Deleting files always asks you first.',
  timeout_s: 30,
  allow_always: false,
  rule_kinds: [],
  folder: null,
  reversible: true,
};

/** A read outside the task's scope: the one kind of question a rule can answer. */
const READ: Payload = {
  approval_id: 2,
  tool: 'fs.read_file',
  prompt: 'Read C:\\Users\\sam\\Documents\\Manuals\\printer-setup.pdf',
  why: 'The printer model is in the manual, and the manual is outside the Work Files scope.',
  tier: 'CAUTION',
  reason: 'This would read something outside the task’s scope.',
  timeout_s: 60,
  allow_always: true,
  rule_kinds: ['exact', 'tool_in_folder', 'tool_for_task'],
  folder: 'c:\\users\\sam\\documents\\manuals',
  reversible: false,
};

function requested(payload: Payload): StreamEvent {
  return {
    seq: 1,
    ts: new Date().toISOString(),
    task_id: '7',
    type: 'approval.requested',
    payload,
  };
}

/** A story is an event on the stream plus the dialog's own state; nothing is faked. */
function state(payload: Payload | null, ui: Partial<ApprovalUiSnapshot> = {}): () => void {
  return () => {
    useStreamStore.setState({
      ...INITIAL_STREAM,
      connection: 'live',
      events: payload === null ? [] : [requested(payload)],
      lastSeq: payload === null ? 0 : 1,
    });
    useApprovalStore.setState({ ...INITIAL_APPROVAL_UI, ...ui });
  };
}

const aegis: AegisStoryParameters = {
  notApplicable: {
    Empty: 'With no approval pending, the dialog does not exist: it renders nothing at all.',
  },
};

const meta = {
  title: 'Approvals/ApprovalDialog',
  component: ApprovalDialog,
  parameters: { aegis, layout: 'fullscreen' },
} satisfies Meta<typeof ApprovalDialog>;

export default meta;

type Story = StoryObj<typeof meta>;

/** `UI.md § 5` as drawn: a dangerous, recoverable deletion, past its input guard. */
export const Default: Story = {
  beforeEach: state(DELETE, { armed: 1 }),
};

/** The first 200 ms: Deny is live and focused, Allow is not. */
export const Guarded: Story = {
  beforeEach: state(DELETE),
};

/** The answer is on its way; nothing can be pressed twice. */
export const Loading: Story = {
  beforeEach: state(DELETE, { armed: 1, sending: { id: 1, choice: 'allow' } }),
};

/** The answer did not land. The core will deny it when its timer runs out. */
export const Error: Story = {
  beforeEach: state(DELETE, {
    armed: 1,
    notice: {
      id: 1,
      text: 'Aegis could not send your answer. If it does not get one, it denies this when the timer runs out.',
    },
  }),
};

/** A caution-tier read that cannot be undone, with *Allow always* on offer. */
export const ReadOutsideScope: Story = {
  beforeEach: state(READ, { armed: 2 }),
};

/** A long command, a long reason and a deep folder, in the dialog's 480px. */
export const LongestContent: Story = {
  beforeEach: state(
    {
      ...READ,
      tool: 'shell.run_powershell',
      tier: 'DANGEROUS',
      allow_always: false,
      rule_kinds: [],
      prompt:
        'Run a command: Get-ChildItem -Path "C:\\Users\\sam\\OneDrive - Contoso Ltd\\Projects\\2026\\Quarterly Reporting\\Regional Breakdowns\\EMEA" -Recurse -Filter *.xlsx | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-365) } | ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination "D:\\Archive\\Quarterly Reporting\\EMEA\\$($_.Directory.Name)" -Force }',
      why: 'The user asked to move every regional spreadsheet older than a year into the archive drive, keeping the folder structure so the finance team can still find them by region and quarter.',
    },
    { armed: 2 },
  ),
};
