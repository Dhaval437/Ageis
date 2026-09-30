import {
  RISK_TIERS,
  RULE_KINDS,
  type ApprovalChoice,
  type BridgeResult,
  type CoreResponse,
  type RiskTier,
  type RuleKind,
  type StreamEvent,
} from '@aegis/shared';

/**
 * The approval dialog's data (`UI.md § 5`, `P3-12`), derived from the event stream
 * and nothing else (REMEMBER.md invariant 15).
 *
 * An approval is **pending** from its `approval.requested` until its
 * `approval.resolved`. The core publishes exactly one of the latter for every
 * approval — answered, timed out, stopped, cancelled — so the dialog closes itself
 * however the question ends, and never shows a question nobody is waiting on.
 *
 * Every field is checked here: the payload crossed IPC, and `prompt` and `why`
 * carry text that can come from the screen. An event that does not parse is never
 * shown as a question and can never be allowed: the dialog says it could not read
 * it and offers only *Deny* (`unreadableApprovals`), and the core's timer denies it
 * otherwise — the safe way for a malformed question to fail.
 */

export interface PendingApproval {
  readonly id: number;
  readonly taskId: string | null;
  readonly tool: string;
  /** The tool's own `describe()` sentence: the literal thing about to happen. */
  readonly prompt: string;
  /** The agent's reason, quoted verbatim, if it gave one. */
  readonly why: string | null;
  readonly tier: RiskTier;
  /** The Guardian's reason for asking. */
  readonly reason: string;
  /** When the core will deny it on its own, in ms since the epoch. */
  readonly deadline: number;
  readonly timeoutS: number;
  readonly allowAlways: boolean;
  readonly ruleKinds: readonly RuleKind[];
  /** The folder a *this tool in this folder* rule would cover. */
  readonly folder: string | null;
  /** `true` only when the tool has a real `undo()`. */
  readonly reversible: boolean;
}

const TIERS: ReadonlySet<string> = new Set(RISK_TIERS);
const KINDS: ReadonlySet<string> = new Set(RULE_KINDS);

function optionalString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'string' ? value : undefined;
}

/** A well-formed `approval.requested`, or `null`. */
export function parseApprovalRequest(event: StreamEvent): PendingApproval | null {
  if (event.type !== 'approval.requested') return null;
  const p = event.payload;
  const id = p['approval_id'];
  const timeoutS = p['timeout_s'];
  const tier = p['tier'];
  const kinds = p['rule_kinds'];
  const why = optionalString(p['why']);
  const folder = optionalString(p['folder']);
  const requestedAt = Date.parse(event.ts);
  if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1) return null;
  if (typeof p['tool'] !== 'string' || typeof p['prompt'] !== 'string') return null;
  if (typeof p['reason'] !== 'string' || typeof tier !== 'string' || !TIERS.has(tier)) return null;
  if (typeof timeoutS !== 'number' || !Number.isFinite(timeoutS) || timeoutS < 0) return null;
  if (typeof p['allow_always'] !== 'boolean' || typeof p['reversible'] !== 'boolean') return null;
  if (!Array.isArray(kinds) || !kinds.every((k) => typeof k === 'string' && KINDS.has(k))) {
    return null;
  }
  if (why === undefined || folder === undefined || Number.isNaN(requestedAt)) return null;
  return {
    id,
    taskId: event.task_id,
    tool: p['tool'],
    prompt: p['prompt'],
    why,
    tier: tier as RiskTier,
    reason: p['reason'],
    deadline: requestedAt + timeoutS * 1000,
    timeoutS,
    // Never offer a rule the core said it would refuse, whatever the list says.
    allowAlways: p['allow_always'] && kinds.length > 0,
    ruleKinds: p['allow_always'] ? (kinds as RuleKind[]) : [],
    folder,
    reversible: p['reversible'],
  };
}

/** Open questions by id, oldest first; `null` for one that did not parse. */
function openApprovals(events: readonly StreamEvent[]): Map<number, PendingApproval | null> {
  const open = new Map<number, PendingApproval | null>();
  for (const event of events) {
    if (event.type !== 'approval.requested' && event.type !== 'approval.resolved') continue;
    const id = event.payload['approval_id'];
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1) continue;
    if (event.type === 'approval.requested') open.set(id, parseApprovalRequest(event));
    else open.delete(id);
  }
  return open;
}

/** Every approval still waiting for an answer, oldest first. */
export function pendingApprovals(events: readonly StreamEvent[]): PendingApproval[] {
  return [...openApprovals(events).values()].filter((approval) => approval !== null);
}

/**
 * The ids of open questions this build could not read, oldest first (P3-19). MAIN
 * shows the approval window for any open question, so the dialog has to say something
 * for these too; all it offers is *Deny*.
 */
export function unreadableApprovals(events: readonly StreamEvent[]): number[] {
  return [...openApprovals(events)].filter(([, approval]) => approval === null).map(([id]) => id);
}

/** Whole seconds left before the core denies it; never negative. */
export function secondsLeft(approval: PendingApproval, now: number): number {
  return Math.max(0, Math.ceil((approval.deadline - now) / 1000));
}

/** The label for each *Allow always ▾* choice (`UI.md § 5`). */
export function ruleLabel(kind: RuleKind, approval: PendingApproval): string {
  switch (kind) {
    case 'exact':
      return 'This exact action';
    case 'tool_in_folder':
      return approval.folder === null
        ? 'This tool in this folder'
        : `This tool in ${approval.folder}`;
    case 'tool_for_task':
      return 'This tool, for this task only';
  }
}

export interface AnswerNotice {
  readonly text: string;
}

/**
 * What to say when an answer did not land. A 404 means the question is already
 * closed; anything else means the answer may not have arrived, and the honest
 * thing to say is what happens then: the core denies it.
 */
export function answerFailure(result: BridgeResult<CoreResponse>): AnswerNotice | null {
  if (result.ok && result.value.status >= 200 && result.value.status < 300) return null;
  // MAIN refused it before it reached the core (`approval-policy.ts`). Its sentences
  // are MAIN's own, never text from the page or the screen.
  if (!result.ok && result.error.code === 'invalid_request' && result.error.message.length <= 200) {
    return { text: result.error.message };
  }
  if (result.ok && result.value.status === 404) {
    return {
      text: 'This question is already closed: it was answered, or it timed out and was denied.',
    };
  }
  if (result.ok && result.value.status === 400) {
    const detail = (result.value.body as { detail?: unknown } | null)?.detail;
    // A 400 here is a sentence the core wrote for this dialog.
    if (typeof detail === 'string' && detail.length > 0 && detail.length <= 200) {
      return { text: detail };
    }
  }
  return {
    text: 'Aegis could not send your answer. If it does not get one, it denies this when the timer runs out.',
  };
}

/**
 * `POST /v1/approvals/{id}`, through the approval window's own bridge (P3-19), which
 * MAIN checks again before it reaches the core. Resolves, never rejects.
 */
export async function sendAnswer(
  id: number,
  choice: ApprovalChoice,
  rule: RuleKind | null = null,
): Promise<BridgeResult<CoreResponse>> {
  const bridge = typeof window === 'undefined' ? undefined : window.aegisApproval;
  if (bridge === undefined) {
    return { ok: false, error: { code: 'unavailable', message: 'no approval bridge' } };
  }
  try {
    return await bridge.answer(id, choice, rule);
  } catch {
    return { ok: false, error: { code: 'failed', message: 'bridge threw' } };
  }
}
