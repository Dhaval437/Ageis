import { RULE_KINDS, type AllowRuleInfo, type RuleKind } from '@aegis/shared';

/**
 * Runtime checks for what `GET /v1/rules` and `DELETE /v1/rules/{id}` answer (`P3-18`),
 * and the plain-English line each rule is shown with (`UI.md § 8.3`, § 11).
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isKind(value: unknown): value is RuleKind {
  return typeof value === 'string' && (RULE_KINDS as readonly string[]).includes(value);
}

function stringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

export function parseRule(value: unknown): AllowRuleInfo | null {
  if (!isRecord(value)) return null;
  const { id, kind, tool, folder, task_id: taskId, created_at: createdAt } = value;
  if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1) return null;
  if (!isKind(kind) || typeof tool !== 'string' || typeof createdAt !== 'string') return null;
  if (!stringOrNull(folder) || !stringOrNull(taskId)) return null;
  // The kind decides which field names the rule's reach; one without it is not a rule.
  if (kind === 'tool_in_folder' && (folder === null || folder === '')) return null;
  if (kind === 'tool_for_task' && (taskId === null || taskId === '')) return null;
  return { id, kind, tool, folder, task_id: taskId, created_at: createdAt };
}

export function parseRuleList(value: unknown): AllowRuleInfo[] | null {
  if (!isRecord(value) || !Array.isArray(value['rules'])) return null;
  const rules = (value['rules'] as unknown[]).map(parseRule);
  return rules.every((rule): rule is AllowRuleInfo => rule !== null) ? rules : null;
}

/**
 * What the rule lets Aegis do without asking, in the words the approval dialog used
 * when the person made it (`lib/approvals.ts` `ruleLabel`). The folder and task are
 * rendered separately, so this line never carries a path.
 */
export function describeRule(rule: Pick<AllowRuleInfo, 'kind'>): string {
  switch (rule.kind) {
    case 'exact':
      return 'This exact action, with the same details you approved.';
    case 'tool_in_folder':
      return 'This tool, for anything inside one folder.';
    case 'tool_for_task':
      return 'This tool, for one task only.';
  }
}

/** `created_at` (ISO, UTC) as a local date and time; the raw text if it will not parse. */
export function formatCreated(createdAt: string, locale?: string): string {
  const moment = new Date(createdAt);
  if (Number.isNaN(moment.getTime())) return createdAt;
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(
    moment,
  );
}
