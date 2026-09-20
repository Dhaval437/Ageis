import type {
  BudgetLimitsSpec,
  ModelCatalog,
  ModelInfo,
  ModelSettings,
  ProviderCatalog,
  ProviderId,
  RoleRouteSpec,
  SpendResponse,
  SpendTotals,
  ValidateResponse,
} from '@aegis/shared';
import { PROVIDER_IDS } from '@aegis/shared';

/**
 * Runtime checks for what `core.request` brings back for the Models screen
 * (`UI.md § 8.4`).
 *
 * The types in `@aegis/shared` are generated from the core's Pydantic models, so
 * they say what the core *means* to send — but the bridge hands the renderer an
 * `unknown` body, and between the two sits an IPC boundary that enforces
 * nothing. `lib/stream-event.ts` makes the same argument about events; this is
 * the same rule applied to responses, so nothing unchecked reaches a store.
 *
 * A shape that does not match is `null`, never a guess: the screen shows its
 * error state, which is honest, rather than rendering half a catalogue.
 */

const PROVIDER_ID_SET: ReadonlySet<string> = new Set(PROVIDER_IDS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function nullableStr(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === 'string' ? value : undefined;
}

function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** `null` is a real value here — an unknown price, or a ceiling that is off. */
function nullableNum(value: unknown): number | null | undefined {
  if (value === null) return null;
  return num(value) ?? undefined;
}

function providerId(value: unknown): ProviderId | null {
  return typeof value === 'string' && PROVIDER_ID_SET.has(value) ? (value as ProviderId) : null;
}

function parseModelInfo(value: unknown): ModelInfo | null {
  if (!isRecord(value)) return null;
  const id = str(value['id']);
  const vision = bool(value['vision']);
  const toolCalling = bool(value['tool_calling']);
  const jsonMode = bool(value['json_mode']);
  const ctxWindow = num(value['ctx_window']);
  const costIn = nullableNum(value['cost_per_mtok_input']);
  const costOut = nullableNum(value['cost_per_mtok_output']);
  if (id === null || vision === null || toolCalling === null || jsonMode === null) return null;
  if (ctxWindow === null || costIn === undefined || costOut === undefined) return null;
  return {
    id,
    vision,
    tool_calling: toolCalling,
    json_mode: jsonMode,
    ctx_window: ctxWindow,
    cost_per_mtok_input: costIn,
    cost_per_mtok_output: costOut,
  };
}

function parseProviderCatalog(value: unknown): ProviderCatalog | null {
  if (!isRecord(value)) return null;
  const id = providerId(value['provider_id']);
  const label = str(value['label']);
  const local = bool(value['local']);
  const requiresKey = bool(value['requires_key']);
  const hasKey = bool(value['has_key']);
  const maskedKey = nullableStr(value['masked_key']);
  const baseUrl = nullableStr(value['base_url']);
  const editableBaseUrl = bool(value['editable_base_url']);
  const freeText = bool(value['free_text_model']);
  const detail = nullableStr(value['detail']);
  const rawModels = value['models'];
  if (id === null || label === null || local === null || requiresKey === null) return null;
  if (hasKey === null || editableBaseUrl === null || freeText === null) return null;
  if (maskedKey === undefined || baseUrl === undefined || detail === undefined) return null;
  if (!Array.isArray(rawModels)) return null;
  const models: ModelInfo[] = [];
  for (const entry of rawModels) {
    const model = parseModelInfo(entry);
    if (model === null) return null;
    models.push(model);
  }
  return {
    provider_id: id,
    label,
    local,
    requires_key: requiresKey,
    has_key: hasKey,
    masked_key: maskedKey,
    base_url: baseUrl,
    editable_base_url: editableBaseUrl,
    models,
    free_text_model: freeText,
    detail,
  };
}

export function parseCatalog(value: unknown): ModelCatalog | null {
  if (!isRecord(value) || !Array.isArray(value['providers'])) return null;
  const providers: ProviderCatalog[] = [];
  for (const entry of value['providers']) {
    const provider = parseProviderCatalog(entry);
    if (provider === null) return null;
    providers.push(provider);
  }
  return { providers };
}

function parseLimits(value: unknown): BudgetLimitsSpec | null {
  if (!isRecord(value)) return null;
  const taskCents = nullableNum(value['task_cents']);
  const dayCents = nullableNum(value['day_cents']);
  const taskTokens = nullableNum(value['task_tokens']);
  const dayTokens = nullableNum(value['day_tokens']);
  if (taskCents === undefined || dayCents === undefined) return null;
  if (taskTokens === undefined || dayTokens === undefined) return null;
  return {
    task_cents: taskCents,
    day_cents: dayCents,
    task_tokens: taskTokens,
    day_tokens: dayTokens,
  };
}

function parseRoute(value: unknown): RoleRouteSpec | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !Array.isArray(value['fallbacks'])) return undefined;
  const primary = parseChoice(value['primary']);
  if (primary === null) return undefined;
  const fallbacks = [];
  for (const entry of value['fallbacks']) {
    const choice = parseChoice(entry);
    if (choice === null) return undefined;
    fallbacks.push(choice);
  }
  return { primary, fallbacks };
}

function parseChoice(value: unknown): RoleRouteSpec['primary'] | null {
  if (!isRecord(value)) return null;
  const id = providerId(value['provider_id']);
  const model = str(value['model']);
  const temperature = nullableNum(value['temperature']);
  const maxOutput = nullableNum(value['max_output_tokens']);
  if (id === null || model === null) return null;
  if (temperature === undefined || maxOutput === undefined) return null;
  return {
    provider_id: id,
    model,
    temperature,
    max_output_tokens: maxOutput,
  };
}

export function parseSettings(value: unknown): ModelSettings | null {
  if (!isRecord(value) || !isRecord(value['models'])) return null;
  const models = value['models'];
  const planner = parseRoute(models['planner']);
  const grounder = parseRoute(models['grounder']);
  const utility = parseRoute(models['utility']);
  const limits = parseLimits(models['limits']);
  const customBaseUrl = nullableStr(models['custom_base_url']);
  const ollamaBaseUrl = nullableStr(models['ollama_base_url']);
  if (planner === undefined || grounder === undefined || utility === undefined) return null;
  if (limits === null || customBaseUrl === undefined || ollamaBaseUrl === undefined) return null;
  return {
    planner,
    grounder,
    utility,
    limits,
    custom_base_url: customBaseUrl,
    ollama_base_url: ollamaBaseUrl,
  };
}

export function parseSpendTotals(value: unknown): SpendTotals | null {
  if (!isRecord(value)) return null;
  const cents = num(value['cents']);
  const tokens = num(value['tokens']);
  const unpriced = num(value['unpriced_calls']);
  if (cents === null || tokens === null || unpriced === null) return null;
  return { cents, tokens, unpriced_calls: unpriced };
}

export function parseSpend(value: unknown): SpendResponse | null {
  if (!isRecord(value)) return null;
  const day = parseSpendTotals(value['day']);
  const limits = parseLimits(value['limits']);
  return day === null || limits === null ? null : { day, limits };
}

export function parseValidate(value: unknown): ValidateResponse | null {
  if (!isRecord(value)) return null;
  const id = providerId(value['provider_id']);
  const valid = bool(value['valid']);
  const detail = str(value['detail']);
  const latency = num(value['latency_ms']);
  if (id === null || valid === null || detail === null || latency === null) return null;
  return { provider_id: id, valid, detail, latency_ms: latency };
}

/**
 * The `detail` string a core error carries, when it carries one the user can act
 * on. A 4xx from the core is a sentence written for them (`P1-05`, `P1-07`); any
 * other status is not, so it never reaches the screen verbatim.
 */
export function coreDetail(status: number, body: unknown): string | null {
  if (status < 400 || status >= 500) return null;
  if (!isRecord(body)) return null;
  const detail = str(body['detail']);
  return detail !== null && detail.trim() !== '' ? detail : null;
}
