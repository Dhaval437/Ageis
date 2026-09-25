import type { Meta, StoryObj } from '@storybook/react-vite';
import type { CoreConnection, StreamEvent } from '@aegis/shared';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { OverlayHUD } from './OverlayHUD';

type Payload = StreamEvent['payload'];

let seq = 0;

function event(type: StreamEvent['type'], payload: Payload): StreamEvent {
  seq += 1;
  return { seq, ts: new Date().toISOString(), task_id: '7', type, payload };
}

const running = event('task.status', { status: 'RUNNING' });

/** Every story is a stream snapshot; the HUD has no state of its own to fake. */
function stream(connection: CoreConnection | null, events: StreamEvent[] = []): () => void {
  return () => {
    useStreamStore.setState({
      ...INITIAL_STREAM,
      connection,
      hasBeenLive: connection === 'live',
      events,
      lastSeq: events.at(-1)?.seq ?? 0,
    });
  };
}

const meta: Meta<typeof OverlayHUD> = {
  title: 'Overlay/OverlayHUD',
  component: OverlayHUD,
  decorators: [
    (Story) => (
      // The HUD's window is 420×64 (`overlay-policy.ts`).
      <div style={{ width: 420, height: 64 }}>
        <Story />
      </div>
    ),
  ],
};

export default meta;

type Story = StoryObj<typeof meta>;

/** `UI.md § 6` as drawn: the agent at work, saying what it is doing. */
export const Default: Story = {
  beforeEach: stream('live', [
    running,
    event('step.action', { summary: 'Clicking "Date modified"…' }),
  ]),
};

/** Before the stream has said anything. */
export const Loading: Story = {
  beforeEach: stream(null),
};

/** Nothing running and nothing asked: the resting state. */
export const Empty: Story = {
  beforeEach: stream('live'),
};

/** The engine went away: red, and nothing to stop. */
export const Error: Story = {
  beforeEach: stream('unavailable'),
};

/** A question waiting: amber, with Deny and Review — never Allow. */
export const WaitingForApproval: Story = {
  beforeEach: stream('live', [
    running,
    event('approval.requested', {
      approval_id: 1,
      tool: 'fs.delete',
      prompt: 'Delete 12 files in D:\\work\\invoices\\archive\\',
      why: null,
      tier: 'DANGEROUS',
      reason: 'Deleting files always asks you first.',
      timeout_s: 30,
      allow_always: false,
      rule_kinds: [],
      folder: null,
      reversible: true,
    }),
  ]),
};

/** The person took over (`UI.md § 7`). */
export const Paused: Story = {
  beforeEach: stream('live', [event('task.status', { status: 'PAUSED_BY_USER' })]),
};

/** The kill switch was pressed. */
export const Stopped: Story = {
  beforeEach: stream('live', [event('task.status', { status: 'STOPPED' })]),
};

/** The longest action text, truncated to one line with the whole of it in the tooltip. */
export const LongestContent: Story = {
  beforeEach: stream('live', [
    running,
    event('step.action', {
      summary:
        'Typing the reconciled Q3 totals for the EMEA, APAC and Americas regions into the "Summary" sheet of Quarterly Reporting 2026 (final, reviewed).xlsx',
    }),
  ]),
};
