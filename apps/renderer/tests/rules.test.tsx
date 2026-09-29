import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AllowRuleInfo, BridgeResult, CoreRequest, CoreResponse } from '@aegis/shared';
import { AlwaysAllowRules } from '@/components/AlwaysAllowRules';
import { describeRule, formatCreated, parseRule, parseRuleList } from '@/lib/rules-api';
import { loadRules, revokeRule } from '@/lib/rules-client';
import { INITIAL_RULES, useRulesStore } from '@/stores/rules';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';

/** The Rules screen's *Always allow* list (P3-18): read the rules, revoke one. */

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

function ok(status: number, body: unknown): BridgeResult<CoreResponse> {
  return { ok: true, value: { status, body } };
}

/** A bridge whose `core.request` answers by method, recording every request. */
function fakeBridge(answers: {
  get?: BridgeResult<CoreResponse>;
  del?: BridgeResult<CoreResponse>;
}) {
  const request = vi.fn((req: CoreRequest) =>
    Promise.resolve(
      req.method === 'DELETE'
        ? (answers.del ?? ok(200, { rules: [] }))
        : (answers.get ?? ok(200, { rules: [FOR_TASK, IN_FOLDER, EXACT] })),
    ),
  );
  return { core: { request, subscribe: vi.fn(), restart: vi.fn() } };
}

beforeEach(() => {
  useRulesStore.setState({ ...INITIAL_RULES });
  useStreamStore.setState({ ...INITIAL_STREAM });
});

describe('parsing', () => {
  it('accepts every kind and a list of them', () => {
    for (const rule of [EXACT, IN_FOLDER, FOR_TASK]) expect(parseRule(rule)).toEqual(rule);
    expect(parseRuleList({ rules: [EXACT, IN_FOLDER] })).toEqual([EXACT, IN_FOLDER]);
    expect(parseRuleList({ rules: [] })).toEqual([]);
  });

  it.each([
    ['no id', { ...EXACT, id: undefined }],
    ['a zero id', { ...EXACT, id: 0 }],
    ['a fractional id', { ...EXACT, id: 1.5 }],
    ['an unknown kind', { ...EXACT, kind: 'everything' }],
    ['no tool', { ...EXACT, tool: 7 }],
    ['no created_at', { ...EXACT, created_at: null }],
    ['a folder rule without its folder', { ...IN_FOLDER, folder: null }],
    ['a folder rule with an empty folder', { ...IN_FOLDER, folder: '' }],
    ['a task rule without its task', { ...FOR_TASK, task_id: null }],
    ['a folder that is not text', { ...IN_FOLDER, folder: ['d:\\'] }],
    ['an array', [EXACT]],
  ])('refuses %s', (_why, value) => {
    expect(parseRule(value)).toBeNull();
  });

  it('refuses a list with any bad rule in it, or no list at all', () => {
    expect(parseRuleList({ rules: [EXACT, { ...EXACT, kind: 'all' }] })).toBeNull();
    expect(parseRuleList({ scopes: [] })).toBeNull();
    expect(parseRuleList(null)).toBeNull();
  });
});

describe('wording', () => {
  it('says what each kind allows, without a path in it', () => {
    expect(describeRule(EXACT)).toBe('This exact action, with the same details you approved.');
    expect(describeRule(IN_FOLDER)).toBe('This tool, for anything inside one folder.');
    expect(describeRule(FOR_TASK)).toBe('This tool, for one task only.');
  });

  it('shows the creation time locally, and the raw text if it will not parse', () => {
    expect(formatCreated('2026-09-25T09:14:03.120Z', 'en-GB')).toMatch(/25 Sept? 2026/);
    expect(formatCreated('not a time')).toBe('not a time');
  });
});

describe('loadRules', () => {
  it('reads GET /rules into the store', async () => {
    const b = fakeBridge({});
    await loadRules(b);
    expect(b.core.request).toHaveBeenCalledWith({ method: 'GET', path: '/rules' });
    expect(useRulesStore.getState()).toMatchObject({
      status: 'ready',
      rules: [FOR_TASK, IN_FOLDER, EXACT],
      loadError: null,
    });
  });

  it('says the engine is unreachable when the bridge says so', async () => {
    await loadRules(
      fakeBridge({ get: { ok: false, error: { code: 'unavailable', message: 'x' } } }),
    );
    expect(useRulesStore.getState().status).toBe('error');
    expect(useRulesStore.getState().loadError).toMatch(/cannot reach the engine/);
  });

  it("shows the core's own sentence for a refusal, and ours for a 5xx", async () => {
    await loadRules(fakeBridge({ get: ok(400, { detail: 'A sentence for you.' }) }));
    expect(useRulesStore.getState().loadError).toBe('A sentence for you.');
    await loadRules(fakeBridge({ get: ok(503, { detail: 'The rules cannot be read.' }) }));
    expect(useRulesStore.getState().loadError).toBe(
      'Aegis could not read your rules. Restart the engine and try again.',
    );
  });

  it('refuses an answer it cannot read rather than showing part of it', async () => {
    await loadRules(fakeBridge({ get: ok(200, { rules: [{ ...EXACT, kind: 'all' }] }) }));
    expect(useRulesStore.getState()).toMatchObject({ status: 'error', rules: [] });
  });

  it('reports a bridge that throws as a failure, not a crash', async () => {
    const b = fakeBridge({});
    b.core.request.mockRejectedValueOnce(new Error('boom'));
    await loadRules(b);
    expect(useRulesStore.getState().status).toBe('error');
  });
});

describe('revokeRule', () => {
  beforeEach(() => {
    useRulesStore.setState({ status: 'ready', rules: [FOR_TASK, IN_FOLDER, EXACT] });
  });

  it('sends DELETE /rules/{id} and shows the rules that remain', async () => {
    const b = fakeBridge({ del: ok(200, { rules: [FOR_TASK, EXACT] }) });
    await revokeRule(IN_FOLDER, b);
    expect(b.core.request).toHaveBeenCalledWith({ method: 'DELETE', path: '/rules/2' });
    expect(useRulesStore.getState()).toMatchObject({
      rules: [FOR_TASK, EXACT],
      revoking: null,
      notice: { tone: 'info', text: 'Revoked. Aegis will ask before using fs.move again.' },
    });
  });

  it('keeps the rule listed, and says it still applies, when the revoke fails', async () => {
    await revokeRule(IN_FOLDER, fakeBridge({ del: ok(503, { detail: 'x' }) }));
    expect(useRulesStore.getState()).toMatchObject({
      rules: [FOR_TASK, IN_FOLDER, EXACT],
      revoking: null,
      notice: {
        tone: 'error',
        text: 'Aegis could not revoke that rule. It still applies. Try again.',
      },
    });
  });

  it('treats a rule already gone as gone, and reads the list again', async () => {
    const b = fakeBridge({
      del: ok(404, { detail: 'There is no such rule.' }),
      get: ok(200, { rules: [EXACT] }),
    });
    await revokeRule(IN_FOLDER, b);
    expect(b.core.request).toHaveBeenLastCalledWith({ method: 'GET', path: '/rules' });
    expect(useRulesStore.getState()).toMatchObject({
      rules: [EXACT],
      notice: { tone: 'info', text: 'That rule was already revoked.' },
    });
  });

  it('marks the rule in flight while it is being revoked', async () => {
    let finish: (value: BridgeResult<CoreResponse>) => void = () => undefined;
    const b = fakeBridge({});
    b.core.request.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const pending = revokeRule(IN_FOLDER, b);
    expect(useRulesStore.getState().revoking).toBe(2);
    finish(ok(200, { rules: [] }));
    await pending;
    expect(useRulesStore.getState().revoking).toBeNull();
  });
});

describe('<AlwaysAllowRules />', () => {
  function withBridge(b: ReturnType<typeof fakeBridge>): void {
    Object.defineProperty(window, 'aegis', { value: b, configurable: true, writable: true });
  }

  it('reads the rules once the engine is live, and not before', async () => {
    const b = fakeBridge({});
    withBridge(b);
    render(<AlwaysAllowRules />);
    expect(b.core.request).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Loading rules')).toBeInTheDocument();
    await act(async () => {
      useStreamStore.setState({ connection: 'live' });
      await Promise.resolve();
    });
    expect(b.core.request).toHaveBeenCalledWith({ method: 'GET', path: '/rules' });
    expect(await screen.findByLabelText('Always-allow rules')).toBeInTheDocument();
  });

  it('shows what, where and when for each kind', () => {
    useRulesStore.setState({ status: 'ready', rules: [FOR_TASK, IN_FOLDER, EXACT] });
    render(<AlwaysAllowRules />);
    const folderRow = screen.getByRole('article', { name: /^fs\.move:/ });
    expect(within(folderRow).getByText('d:\\work\\invoices')).toBeInTheDocument();
    expect(within(folderRow).getByText(/^Made /)).toBeInTheDocument();
    const taskRow = screen.getByRole('article', { name: /^fs\.write:/ });
    expect(within(taskRow).getByText('task-7f3c')).toBeInTheDocument();
    const exactRow = screen.getByRole('article', { name: /^input\.click:/ });
    expect(within(exactRow).getByText(/same details you approved/)).toBeInTheDocument();
  });

  it('revokes the rule whose button was pressed, and locks the others meanwhile', async () => {
    useRulesStore.setState({ status: 'ready', rules: [FOR_TASK, IN_FOLDER] });
    const b = fakeBridge({});
    let finish: (value: BridgeResult<CoreResponse>) => void = () => undefined;
    b.core.request.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    withBridge(b);
    render(<AlwaysAllowRules />);
    fireEvent.click(screen.getByRole('button', { name: 'Revoke the rule for fs.move' }));
    expect(b.core.request).toHaveBeenCalledWith({ method: 'DELETE', path: '/rules/2' });
    expect(screen.getByRole('button', { name: 'Revoke the rule for fs.move' })).toHaveTextContent(
      'Revoking…',
    );
    expect(screen.getByRole('button', { name: 'Revoke the rule for fs.write' })).toBeDisabled();
    await act(async () => {
      finish(ok(200, { rules: [FOR_TASK] }));
      await Promise.resolve();
    });
    expect(screen.queryByRole('article', { name: /^fs\.move:/ })).toBeNull();
    expect(screen.getByText(/Aegis will ask before using fs\.move again/)).toBeInTheDocument();
  });

  it('says there are no rules, in words about what that means', () => {
    useRulesStore.setState({ status: 'ready' });
    render(<AlwaysAllowRules />);
    expect(
      screen.getByText(/No rules yet\. Aegis asks you before every action/),
    ).toBeInTheDocument();
  });

  it('shows a load failure as an alert with a way to try again', async () => {
    useRulesStore.setState({ status: 'error', loadError: 'Aegis could not read your rules.' });
    const b = fakeBridge({});
    withBridge(b);
    render(<AlwaysAllowRules />);
    expect(screen.getByRole('alert')).toHaveTextContent('Aegis could not read your rules.');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
      await Promise.resolve();
    });
    expect(b.core.request).toHaveBeenCalledWith({ method: 'GET', path: '/rules' });
  });
});
