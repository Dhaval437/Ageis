import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, CoreResponse, StreamEvent } from '@aegis/shared';
import { ApprovalDialog, INPUT_GUARD_MS } from '@/components/ApprovalDialog';
import {
  answerFailure,
  parseApprovalRequest,
  pendingApprovals,
  ruleLabel,
  secondsLeft,
  unreadableApprovals,
} from '@/lib/approvals';
import { INITIAL_APPROVAL_UI, useApprovalStore } from '@/stores/approval';
import { INITIAL_STREAM, reduceStream, useStreamStore } from '@/stores/stream';

/**
 * The approval dialog (`UI.md § 5`, `P3-12`): what it shows, the rules it keeps
 * (Deny first and always live, a 200 ms guard on Allow, Esc denies, a focus trap,
 * a banner that never lies), and that it is a function of the stream.
 */

type Payload = StreamEvent['payload'];

const NOW = Date.parse('2026-09-25T12:00:00.000Z');

const BASE: Payload = {
  approval_id: 1,
  tool: 'fs.delete',
  prompt: 'Delete 12 files in D:\\work\\invoices\\archive\\',
  why: "The user asked to clear last year's archive.",
  tier: 'DANGEROUS',
  reason: 'Deleting files always asks you first.',
  timeout_s: 30,
  allow_always: false,
  rule_kinds: [],
  folder: null,
  reversible: true,
};

let seq = 0;

function requested(payload: Payload = BASE, ts = new Date(NOW).toISOString()): StreamEvent {
  seq += 1;
  return { seq, ts, task_id: '7', type: 'approval.requested', payload };
}

function resolved(id: number): StreamEvent {
  seq += 1;
  return {
    seq,
    ts: new Date(NOW).toISOString(),
    task_id: '7',
    type: 'approval.resolved',
    payload: { approval_id: id, choice: 'deny', decided_by: 'timeout', rule_id: null },
  };
}

function show(...events: StreamEvent[]): void {
  act(() => {
    for (const event of events) {
      useStreamStore.setState((state) => reduceStream(state, { kind: 'event', event }));
    }
  });
}

const answered: BridgeResult<CoreResponse> = {
  ok: true,
  value: { status: 200, body: { approval_id: 1, choice: 'allow', rule_id: null } },
};
/** `window.aegisApproval.answer` (P3-19): the dialog's only way to answer. */
const request = vi.fn(
  (_id: number, _choice: string, _rule: string | null): Promise<BridgeResult<CoreResponse>> =>
    Promise.resolve(answered),
);

beforeEach(() => {
  vi.useFakeTimers({ now: NOW });
  request.mockClear();
  request.mockResolvedValue(answered);
  useStreamStore.setState(INITIAL_STREAM);
  useApprovalStore.setState(INITIAL_APPROVAL_UI);
  vi.stubGlobal('aegisApproval', { answer: request, subscribe: vi.fn() });
});

afterEach(() => {
  vi.useRealTimers();
});

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

describe('ApprovalDialog', () => {
  it('is not there with nothing pending', () => {
    const { container } = render(<ApprovalDialog />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the literal thing, the reason, the tier as a word, and the banner', () => {
    render(<ApprovalDialog />);
    show(requested());
    const dialog = screen.getByRole('alertdialog', { name: 'Aegis needs your approval' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText('Delete 12 files in D:\\work\\invoices\\archive\\')).toBeVisible();
    expect(screen.getByText("The user asked to clear last year's archive.")).toBeVisible();
    expect(screen.getByText('Dangerous')).toBeVisible();
    expect(screen.getByText('Recoverable: Aegis can undo this afterwards.')).toBeVisible();
    expect(screen.getByText('Deny in 30s')).toBeVisible();
  });

  it('says it cannot be undone unless the core says otherwise', () => {
    render(<ApprovalDialog />);
    show(requested({ ...BASE, reversible: false }));
    expect(screen.getByText('This cannot be undone.')).toBeVisible();
    expect(screen.queryByText(/Recoverable/)).toBeNull();
  });

  it('puts focus on Deny, and keeps Allow disabled for the input guard', () => {
    render(<ApprovalDialog />);
    show(requested());
    const deny = screen.getByRole('button', { name: 'Deny' });
    const allow = screen.getByRole('button', { name: 'Allow once' });
    expect(deny).toHaveFocus();
    expect(deny).toBeEnabled();
    expect(allow).toBeDisabled();
    act(() => {
      vi.advanceTimersByTime(INPUT_GUARD_MS - 1);
    });
    expect(allow).toBeDisabled();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(allow).toBeEnabled();
  });

  it('a click during the guard approves nothing', () => {
    render(<ApprovalDialog />);
    show(requested());
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }));
    expect(request).not.toHaveBeenCalled();
  });

  it('allows once after the guard, through the core', async () => {
    render(<ApprovalDialog />);
    show(requested());
    act(() => {
      vi.advanceTimersByTime(INPUT_GUARD_MS);
    });
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }));
    await settle();
    expect(request).toHaveBeenCalledExactlyOnceWith(1, 'allow', null);
  });

  it('denies on Deny and on Esc, even inside the guard', async () => {
    render(<ApprovalDialog />);
    show(requested());
    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' });
    await settle();
    expect(request).toHaveBeenLastCalledWith(1, 'deny', null);
  });

  it('closes when the core says the question is closed, however it ended', () => {
    render(<ApprovalDialog />);
    show(requested());
    expect(screen.getByRole('alertdialog')).toBeVisible();
    show(resolved(1));
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('asks one question at a time, oldest first, and re-arms the guard for the next', () => {
    render(<ApprovalDialog />);
    show(requested(), requested({ ...BASE, approval_id: 2, prompt: 'Second question' }));
    expect(screen.queryByText('Second question')).toBeNull();
    act(() => {
      vi.advanceTimersByTime(INPUT_GUARD_MS);
    });
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeEnabled();
    show(resolved(1));
    expect(screen.getByText('Second question')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deny' })).toHaveFocus();
  });

  it('traps focus inside the dialog', () => {
    render(<ApprovalDialog />);
    show(requested());
    act(() => {
      vi.advanceTimersByTime(INPUT_GUARD_MS);
    });
    const dialog = screen.getByRole('alertdialog');
    const deny = screen.getByRole('button', { name: 'Deny' });
    const allow = screen.getByRole('button', { name: 'Allow once' });
    allow.focus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(deny).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(allow).toHaveFocus();
  });

  it('offers Allow always only when the core does, with its scopes', async () => {
    render(<ApprovalDialog />);
    show(requested());
    expect(screen.queryByRole('button', { name: /Allow always/ })).toBeNull();
    show(resolved(1));
    show(
      requested({
        ...BASE,
        approval_id: 2,
        tier: 'CAUTION',
        allow_always: true,
        rule_kinds: ['exact', 'tool_in_folder', 'tool_for_task'],
        folder: 'c:\\docs',
      }),
    );
    act(() => {
      vi.advanceTimersByTime(INPUT_GUARD_MS);
    });
    fireEvent.click(screen.getByRole('button', { name: /Allow always/ }));
    expect(screen.getByRole('button', { name: 'This exact action' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'This tool, for this task only' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'This tool in c:\\docs' }));
    await settle();
    expect(request).toHaveBeenCalledExactlyOnceWith(2, 'allow_always', 'tool_in_folder');
  });

  it('says so, assertively, when an answer does not land', async () => {
    request.mockResolvedValue({ ok: false, error: { code: 'unavailable', message: 'x' } });
    render(<ApprovalDialog />);
    show(requested());
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await settle();
    const notice = screen.getByText(/could not send your answer/);
    expect(notice.closest('[aria-live="assertive"]')).not.toBeNull();
  });

  it("shows MAIN's own sentence when MAIN refused the answer", async () => {
    request.mockResolvedValue({
      ok: false,
      error: {
        code: 'invalid_request',
        message: 'That was too soon after the question appeared. Try again.',
      },
    });
    render(<ApprovalDialog />);
    show(requested());
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await settle();
    expect(
      screen.getByText('That was too soon after the question appeared. Try again.'),
    ).toBeVisible();
  });

  it('says it could not send when there is no approval bridge at all', async () => {
    vi.stubGlobal('aegisApproval', undefined);
    render(<ApprovalDialog />);
    show(requested());
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await settle();
    expect(screen.getByText(/could not send your answer/)).toBeVisible();
  });

  it('says it cannot read a malformed question, shows none of it, and offers only Deny', async () => {
    render(<ApprovalDialog />);
    show(requested({ approval_id: 6, tool: 'fs.delete', prompt: 'Delete everything' }));
    expect(screen.getByRole('alertdialog', { name: 'Aegis needs your approval' })).toBeVisible();
    expect(screen.getByText(/could not read the question/)).toBeVisible();
    expect(screen.queryByText(/Delete everything/)).toBeNull();
    expect(screen.queryByRole('button', { name: /allow/i })).toBeNull();
    const deny = screen.getByRole('button', { name: 'Deny' });
    expect(deny).toHaveFocus();
    fireEvent.click(deny);
    await settle();
    expect(request).toHaveBeenCalledExactlyOnceWith(6, 'deny', null);
    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' });
    await settle();
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('asks what it can read before saying a question is unreadable, and closes with it', () => {
    const { container } = render(<ApprovalDialog />);
    show(requested({ approval_id: 6 }), requested({ ...BASE, approval_id: 7 }));
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeVisible();
    show(resolved(7));
    expect(screen.getByText(/could not read the question/)).toBeVisible();
    show(resolved(6));
    expect(container).toBeEmptyDOMElement();
  });

  it('counts down from the core’s own timestamp', () => {
    render(<ApprovalDialog />);
    show(requested(BASE, new Date(NOW - 10_000).toISOString()));
    expect(screen.getByText('Deny in 20s')).toBeVisible();
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getByText('Deny in 15s')).toBeVisible();
  });

  it('clips a long prompt and shows all of it on request', () => {
    render(<ApprovalDialog />);
    show(requested({ ...BASE, prompt: `Run a command: ${'x'.repeat(400)}` }));
    expect(screen.queryByText(`Run a command: ${'x'.repeat(400)}`)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
    expect(screen.getByText(`Run a command: ${'x'.repeat(400)}`)).toBeVisible();
  });
});

describe('approvals, from the stream', () => {
  const event = (payload: Payload): StreamEvent => requested(payload);

  it.each([
    ['a missing id', { ...BASE, approval_id: undefined }],
    ['a zero id', { ...BASE, approval_id: 0 }],
    ['an unknown tier', { ...BASE, tier: 'SCARY' }],
    ['a string timeout', { ...BASE, timeout_s: '30' }],
    ['a non-boolean flag', { ...BASE, reversible: 'yes' }],
    ['an unknown rule kind', { ...BASE, rule_kinds: ['everything'] }],
    ['a numeric prompt', { ...BASE, prompt: 7 }],
  ])('refuses %s', (_why, payload) => {
    expect(parseApprovalRequest(event(payload as Payload))).toBeNull();
  });

  it('never offers a rule the core said it would refuse', () => {
    const approval = parseApprovalRequest(
      event({ ...BASE, allow_always: false, rule_kinds: ['exact'] }),
    );
    expect(approval?.allowAlways).toBe(false);
    expect(approval?.ruleKinds).toEqual([]);
  });

  it('keeps what is pending in order and drops what was resolved', () => {
    const a = requested();
    const b = requested({ ...BASE, approval_id: 2 });
    expect(pendingApprovals([a, b]).map((p) => p.id)).toEqual([1, 2]);
    expect(pendingApprovals([a, b, resolved(1)]).map((p) => p.id)).toEqual([2]);
  });

  it('lists open questions that do not parse, until they are resolved', () => {
    const events = [
      requested({ approval_id: 6 }),
      requested({ ...BASE, approval_id: 7 }),
      requested({ ...BASE, approval_id: 8, tier: 'HARMLESS' }),
      requested({ ...BASE, approval_id: 'nine' }),
    ];
    expect(unreadableApprovals(events)).toEqual([6, 8]);
    expect(pendingApprovals(events).map((a) => a.id)).toEqual([7]);
    expect(unreadableApprovals([...events, resolved(6)])).toEqual([8]);
  });

  it('never counts below zero', () => {
    const approval = parseApprovalRequest(event(BASE));
    expect(approval).not.toBeNull();
    if (approval !== null) expect(secondsLeft(approval, NOW + 60_000)).toBe(0);
  });

  it('names each rule scope in plain words', () => {
    const approval = parseApprovalRequest(event({ ...BASE, folder: 'c:\\docs' }));
    expect(approval).not.toBeNull();
    if (approval === null) return;
    expect(ruleLabel('tool_in_folder', approval)).toBe('This tool in c:\\docs');
  });

  it.each([
    [{ ok: true, value: { status: 200, body: null } }, null],
    [{ ok: true, value: { status: 404, body: null } }, /already closed/],
    [
      { ok: true, value: { status: 400, body: { detail: 'Allow it once instead.' } } },
      /Allow it once instead\./,
    ],
    [{ ok: true, value: { status: 500, body: { detail: 'Traceback…' } } }, /could not send/],
    [{ ok: false, error: { code: 'unavailable', message: 'x' } }, /could not send/],
  ] as const)('explains a failed answer (%#)', (result, expected) => {
    const notice = answerFailure(result);
    if (expected === null) expect(notice).toBeNull();
    else expect(notice?.text).toMatch(expected);
  });
});
