/**
 * GENERATED FILE — do not edit by hand.
 *
 * `scripts/gen-types.ts` (task P0-11) regenerates this from the Pydantic models in
 * `core/aegis_core/server/schemas.py`. Until then it holds a single placeholder so
 * the package compiles.
 */

/** Risk tiers every tool call is classified into. See REMEMBER.md § 6. */
export type RiskTier = 'SAFE' | 'CAUTION' | 'DANGEROUS' | 'FORBIDDEN';

/*
 * ---------------------------------------------------------------------------
 * HAND-SEEDED until P0-11. The generator's output must replace everything below
 * this line verbatim — same names, same shapes — and P0-11 is not done until it
 * does. Source: `StreamEvent` / `EventType` in `core/aegis_core/server/schemas.py`.
 * ---------------------------------------------------------------------------
 */

/** Every event type on `WS /v1/stream` (`ARCHITECTURE.md § 9.2`). */
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
