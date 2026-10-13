import type {
  AegisBridge,
  AllowRuleInfo,
  AskRuleInfo,
  BridgeResult,
  CoreResponse,
} from '@aegis/shared';
import { coreDetail } from '@/lib/models-api';
import { parseRuleList } from '@/lib/rules-api';
import { useRulesStore, type RulesState } from '@/stores/rules';

/**
 * What the Rules screen asks the core (`P3-18`), and the only writer of the rules store.
 * Both calls go through `core.request`: listing reads, and revoking only takes a
 * permission away, so neither needs anything MAIN alone may send.
 */

type Bridge = Pick<AegisBridge, 'core'>;

const UNREACHABLE = 'Aegis cannot reach the engine. Check the engine status in the title bar.';
const LOAD_FAILED = 'Aegis could not read your rules. Restart the engine and try again.';
const REVOKE_FAILED = 'Aegis could not revoke that rule. It still applies. Try again.';
const UNREADABLE = 'The engine answered with something Aegis could not read.';

function bridge(): Bridge | undefined {
  return typeof window === 'undefined' ? undefined : window.aegis;
}

function set(patch: Partial<RulesState>): void {
  useRulesStore.setState(patch);
}

type Outcome =
  | {
      readonly ok: true;
      readonly rules: AllowRuleInfo[];
      readonly alwaysAsk: AskRuleInfo[];
      readonly never: string[];
    }
  | { readonly ok: false; readonly text: string; readonly status?: number };

async function ask(
  b: Bridge,
  request: Parameters<Bridge['core']['request']>[0],
  fallback: string,
): Promise<Outcome> {
  let result: BridgeResult<CoreResponse>;
  try {
    result = await b.core.request(request);
  } catch {
    return { ok: false, text: fallback };
  }
  if (!result.ok) {
    return { ok: false, text: result.error.code === 'unavailable' ? UNREACHABLE : fallback };
  }
  const { status, body } = result.value;
  if (status < 200 || status >= 300) {
    return { ok: false, text: coreDetail(status, body) ?? fallback, status };
  }
  const dashboard = parseRuleList(body);
  return dashboard === null ? { ok: false, text: UNREADABLE } : { ok: true, ...dashboard };
}

export async function loadRules(b: Bridge | undefined = bridge()): Promise<void> {
  if (b === undefined) return;
  set({ status: 'loading', loadError: null });
  const outcome = await ask(b, { method: 'GET', path: '/rules' }, LOAD_FAILED);
  if (!outcome.ok) {
    set({ status: 'error', loadError: outcome.text });
    return;
  }
  set({
    status: 'ready',
    rules: outcome.rules,
    alwaysAsk: outcome.alwaysAsk,
    never: outcome.never,
    loadError: null,
  });
}

/**
 * Revoke one rule. From then on that action asks again. A 404 means it is already
 * gone, so the list is reloaded rather than reporting a failure that is not one.
 */
export async function revokeRule(
  rule: AllowRuleInfo,
  b: Bridge | undefined = bridge(),
): Promise<void> {
  if (b === undefined) return;
  set({ revoking: rule.id, notice: null });
  const outcome = await ask(
    b,
    { method: 'DELETE', path: `/rules/${String(rule.id)}` },
    REVOKE_FAILED,
  );
  if (outcome.ok) {
    set({
      status: 'ready',
      rules: outcome.rules,
      alwaysAsk: outcome.alwaysAsk,
      never: outcome.never,
      revoking: null,
      notice: { tone: 'info', text: `Revoked. Aegis will ask before using ${rule.tool} again.` },
    });
    return;
  }
  if (outcome.status === 404) {
    set({ revoking: null, notice: { tone: 'info', text: 'That rule was already revoked.' } });
    await loadRules(b);
    return;
  }
  set({ revoking: null, notice: { tone: 'error', text: outcome.text } });
}

export async function addAskRule(tool: string, b: Bridge | undefined = bridge()): Promise<void> {
  if (b === undefined) return;
  set({ changingAsk: tool, notice: null });
  const outcome = await ask(
    b,
    { method: 'POST', path: '/rules/always-ask', body: { tool } },
    'Aegis could not save that rule. Try again.',
  );
  if (!outcome.ok) {
    set({ changingAsk: null, notice: { tone: 'error', text: outcome.text } });
    return;
  }
  set({
    status: 'ready',
    rules: outcome.rules,
    alwaysAsk: outcome.alwaysAsk,
    never: outcome.never,
    changingAsk: null,
    notice: { tone: 'info', text: `Aegis will always ask before using ${tool}.` },
  });
}

export async function removeAskRule(tool: string, b: Bridge | undefined = bridge()): Promise<void> {
  if (b === undefined) return;
  set({ changingAsk: tool, notice: null });
  const outcome = await ask(
    b,
    { method: 'DELETE', path: `/rules/always-ask/${encodeURIComponent(tool)}` },
    'Aegis could not remove that rule. It will keep asking. Try again.',
  );
  if (!outcome.ok) {
    set({ changingAsk: null, notice: { tone: 'error', text: outcome.text } });
    return;
  }
  set({
    status: 'ready',
    rules: outcome.rules,
    alwaysAsk: outcome.alwaysAsk,
    never: outcome.never,
    changingAsk: null,
    notice: { tone: 'info', text: `${tool} now follows your autonomy setting.` },
  });
}
