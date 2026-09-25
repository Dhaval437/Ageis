/**
 * GENERATED FILE — do not edit by hand.
 *
 * Source: `core/aegis_core/server/schemas.py`, rendered by `aegis_core.server.typegen`.
 * Regenerate with `pnpm gen:types`; `pnpm test` fails while this file is stale.
 */

export const PROVIDER_IDS = [
  'openai',
  'anthropic',
  'google',
  'nvidia',
  'openrouter',
  'ollama',
  'custom',
] as const;

export type ProviderId = (typeof PROVIDER_IDS)[number];

/** Risk tiers every tool call is classified into. See `REMEMBER.md § 6`. */
export const RISK_TIERS = ['SAFE', 'CAUTION', 'DANGEROUS', 'FORBIDDEN'] as const;

export type RiskTier = (typeof RISK_TIERS)[number];

/** Every event type on `WS /v1/stream`. Adding one is an `ARCHITECTURE.md § 9.2` edit first. */
export const EVENT_TYPES = [
  'task.created',
  'task.status',
  'step.started',
  'step.thought',
  'step.action',
  'step.observation',
  'approval.requested',
  'approval.resolved',
  'preempt.triggered',
  'error',
  'cost.updated',
  'log',
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/**
 * A task's place in `RECOVERY.md § 3.1`'s state machine: `tasks.status`, and the
 * `status` in every `task.status` event's payload. MAIN's watchdog (`P3-07`) reads it.
 */
export const TASK_STATES = [
  'QUEUED',
  'RUNNING',
  'PAUSED_BY_USER',
  'WAITING_APPROVAL',
  'DONE',
  'FAILED',
  'STOPPED',
  'ABANDONED',
] as const;

export type TaskState = (typeof TASK_STATES)[number];

/**
 * The user's autonomy setting (`ARCHITECTURE.md § 10`). No level lets `DANGEROUS` run
 * unattended; the Guardian enforces that whatever this says.
 */
export const AUTONOMYS = ['observe', 'guided', 'standard', 'trusted'] as const;

export type Autonomy = (typeof AUTONOMYS)[number];

/**
 * What the Guardian decided for one tool call: `steps.decision`. Only `confirm` ever
 * reaches the human; a `deny` is final for that call.
 */
export const DECISIONS = ['allow', 'confirm', 'deny'] as const;

export type Decision = (typeof DECISIONS)[number];

/** What the person answered an approval with: `approvals.choice`. */
export const APPROVAL_CHOICES = ['allow', 'deny', 'allow_always'] as const;

export type ApprovalChoice = (typeof APPROVAL_CHOICES)[number];

/** Who decided: the person, the auto-deny timer, or a stop (kill switch, task ended). */
export const APPROVAL_OUTCOME_SOURCES = ['user', 'timeout', 'stopped'] as const;

export type ApprovalOutcomeSource = (typeof APPROVAL_OUTCOME_SOURCES)[number];

/**
 * An always-allow rule's scope: *this exact action*, *this tool in this folder*, or
 * *this tool for this task only* (`UI.md § 5`). There is no "everything, forever".
 */
export const RULE_KINDS = ['exact', 'tool_in_folder', 'tool_for_task'] as const;

export type RuleKind = (typeof RULE_KINDS)[number];

/** `GET /v1/health` (`ARCHITECTURE.md § 9.1`). */
export interface HealthResponse {
  /** Always `ok`; a core that cannot answer is down. */
  readonly status: 'ok';
  /** The `aegis_core` package version. */
  readonly version: string;
  /** Seconds since the app was created. */
  readonly uptime: number;
}

/**
 * `POST /v1/kill` — the core's acknowledgement of the kill switch (`P3-06`).
 *
 * MAIN reads a `200` inside its deadline as "input has stopped"; anything else, or
 * nothing, and it terminates the core. Counts only: nothing a key name could be in.
 */
export interface KillResponse {
  /** Always `true`; the switch stays engaged. */
  readonly engaged: true;
  /** Keys released across every controller. */
  readonly released_keys: number;
  /** Mouse buttons released. */
  readonly released_buttons: number;
  /** Controllers whose release raised. Non-zero means a key may still be held. */
  readonly release_failures: number;
}

/** One message on `WS /v1/stream` (`ARCHITECTURE.md § 9.2`). */
export interface StreamEvent {
  /** Monotonic within one core process, starting at 1. */
  readonly seq: number;
  /** UTC ISO-8601 with milliseconds. */
  readonly ts: string;
  /** The task this belongs to; `null` if app-wide. */
  readonly task_id: string | null;
  readonly type: EventType;
  /** Type-specific body. */
  readonly payload: Readonly<Record<string, unknown>>;
}

/**
 * One model a provider can serve, as the Models screen lists it.
 *
 * A price of `null` is **unknown, never free** (`models/schemas.py`, `Capabilities`);
 * a local model is `0.0`, which says the price is known and is nothing.
 */
export interface ModelInfo {
  /** The provider's own model id, sent on the wire as-is. */
  readonly id: string;
  readonly vision: boolean;
  readonly tool_calling: boolean;
  readonly json_mode: boolean;
  /** Total tokens the model accepts, in + out. */
  readonly ctx_window: number;
  /** US dollars per million input tokens; `null` is unknown. */
  readonly cost_per_mtok_input: number | null;
  readonly cost_per_mtok_output: number | null;
}

/**
 * One provider card on the Models screen.
 *
 * It never carries a key. `masked_key` is `mask_key()`'s `sk-…abcd` and nothing else
 * (`ARCHITECTURE.md § 5.3`), and it is `null` when no key is saved.
 */
export interface ProviderCatalog {
  readonly provider_id: ProviderId;
  /** What the card is called, e.g. `Local (Ollama)`. */
  readonly label: string;
  /** Runs on this machine: the *Nothing leaves your PC* badge. */
  readonly local: boolean;
  /** `false` for an endpoint with nobody to authenticate. */
  readonly requires_key: boolean;
  readonly has_key: boolean;
  /** `sk-…abcd`, never the key. */
  readonly masked_key: string | null;
  /** The address, where the user may set one. */
  readonly base_url: string | null;
  /** Whether this card shows an address field. */
  readonly editable_base_url: boolean;
  /** What this build knows this provider serves. May be empty. */
  readonly models: ReadonlyArray<ModelInfo>;
  /** No enumerable catalogue: the user types a model id instead of picking one. */
  readonly free_text_model: boolean;
  /** Why the list is empty, when there is a reason worth showing. */
  readonly detail: string | null;
}

/** `GET /v1/models/catalog` — every provider and what it can serve. */
export interface ModelCatalog {
  readonly providers: ReadonlyArray<ProviderCatalog>;
}

/** One model in one slot of one role's chain, as the screen saves it. */
export interface ModelChoiceSpec {
  readonly provider_id: ProviderId;
  readonly model: string;
  readonly temperature: number | null;
  readonly max_output_tokens: number | null;
}

/** Who answers for one role, and who answers when they cannot. */
export interface RoleRouteSpec {
  readonly primary: ModelChoiceSpec;
  readonly fallbacks: ReadonlyArray<ModelChoiceSpec>;
}

/**
 * The four ceilings. An explicit `null` is *no ceiling on this one*.
 *
 * The defaults are `models/budget.py`'s, restated rather than imported because that
 * module already imports this one for `EventType` and a cycle is the worse trade. A
 * test asserts the two agree, the same way `storage/usage.py`'s `UsageRole` is held to
 * `models/router.py`'s `ModelRole`.
 *
 * Restating them also makes an omitted `limits` mean *the defaults* rather than *no
 * ceilings at all*, so a settings document written by an older build cannot silently
 * remove every ceiling the user was relying on.
 */
export interface BudgetLimitsSpec {
  readonly task_cents: number | null;
  readonly day_cents: number | null;
  readonly task_tokens: number | null;
  readonly day_tokens: number | null;
}

/**
 * Everything the Models screen owns, as it is saved and read back.
 *
 * A role is `null` until the user maps it — the state the app is in on first run, and
 * the reason this is not a `RoleMap` (which requires all three).
 */
export interface ModelSettings {
  readonly planner: RoleRouteSpec | null;
  readonly grounder: RoleRouteSpec | null;
  readonly utility: RoleRouteSpec | null;
  readonly limits: BudgetLimitsSpec;
  /** The user's OpenAI-compatible gateway. */
  readonly custom_base_url: string | null;
  /** `null` means the detected default. */
  readonly ollama_base_url: string | null;
}

/** `GET /v1/settings` and the answer to `PUT /v1/settings`. */
export interface SettingsResponse {
  readonly models: ModelSettings;
}

/** The body of `PUT /v1/settings`. Whole sections, never a patch. */
export interface SettingsRequest {
  readonly models: ModelSettings;
}

/**
 * The body of `PUT /v1/models/keys/{provider_id}`.
 *
 * `repr=False` so the key cannot reach a log line, a traceback or an error payload
 * through a model repr — the same guard `ImagePart.data` carries. It is deliberately
 * **unconstrained** here: every refusal comes from `storage/vault.py`, whose messages
 * quote nothing, because a Pydantic validation error echoes the value it rejected.
 */
export interface KeyRequest {
  /** The provider API key. Written, never read back. */
  readonly key: string;
}

/** What a key write answers with: the masked form, never the key. */
export interface KeyResponse {
  readonly provider_id: ProviderId;
  readonly has_key: boolean;
  /** `sk-…abcd`, never the key. */
  readonly masked_key: string | null;
}

/** The body of `POST /v1/models/validate` — the Models screen's *Test* button. */
export interface ValidateRequest {
  readonly provider_id: ProviderId;
}

/** What *Test* found out. `detail` is shown to the user and holds no key material. */
export interface ValidateResponse {
  readonly provider_id: ProviderId;
  readonly valid: boolean;
  /** Plain, second person, no key and no response body. */
  readonly detail: string;
  /** How long the provider took to answer. */
  readonly latency_ms: number;
}

/** What some slice of the usage ledger adds up to (`storage/usage.py`, `Spend`). */
export interface SpendTotals {
  /** The sum of the prices that are known. */
  readonly cents: number;
  readonly tokens: number;
  /** Calls left out of `cents`; non-zero means *at least* that much. */
  readonly unpriced_calls: number;
}

/**
 * `GET /v1/models/spend` — what the spend meter starts from.
 *
 * It is a first value, not a poll: `cost.updated` (`ARCHITECTURE.md § 9.2`) keeps the
 * meter live, and invariant 15 says the UI is a function of the stream.
 */
export interface SpendResponse {
  readonly day: SpendTotals;
  readonly limits: BudgetLimitsSpec;
}

/**
 * `POST /v1/approvals/{id}`: the person's answer.
 *
 * `rule` is required with `allow_always` and refused with anything else. It names a
 * *kind* only: which folder or which task a rule covers is taken from the call being
 * approved, never from the request, so the renderer cannot name a path.
 */
export interface ApprovalDecision {
  readonly choice: ApprovalChoice;
  readonly rule: RuleKind | null;
}

/** The core's answer to `POST /v1/approvals/{id}`. */
export interface ApprovalResolved {
  readonly approval_id: number;
  readonly choice: ApprovalChoice;
  /** The always-allow rule created, if one was. */
  readonly rule_id: number | null;
}

/** One always-allow rule, as the Rules screen lists it. */
export interface AllowRuleInfo {
  readonly id: number;
  readonly kind: RuleKind;
  readonly tool: string;
  /** Canonical folder, for `tool_in_folder`. */
  readonly folder: string | null;
  /** The task, for `tool_for_task`. */
  readonly task_id: string | null;
  readonly created_at: string;
}

/** `GET /v1/rules`: every always-allow rule, newest first. Each is revocable. */
export interface AllowRuleList {
  readonly rules: ReadonlyArray<AllowRuleInfo>;
}
