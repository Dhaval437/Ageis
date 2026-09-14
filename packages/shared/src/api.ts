/**
 * GENERATED FILE — do not edit by hand.
 *
 * Source: `core/aegis_core/server/schemas.py`, rendered by `aegis_core.server.typegen`.
 * Regenerate with `pnpm gen:types`; `pnpm test` fails while this file is stale.
 */

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

/** `GET /v1/health` (`ARCHITECTURE.md § 9.1`). */
export interface HealthResponse {
  /** Always `ok`; a core that cannot answer is down. */
  readonly status: 'ok';
  /** The `aegis_core` package version. */
  readonly version: string;
  /** Seconds since the app was created. */
  readonly uptime: number;
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
