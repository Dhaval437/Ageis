import type { StreamSnapshot } from '@/stores/stream';
import { pendingApprovals, type PendingApproval } from '@/lib/approvals';

/**
 * What the OverlayHUD says (`UI.md § 6`, P3-13), derived from the event stream and
 * nothing else (REMEMBER.md invariant 15).
 *
 * In order of precedence: an engine that is not there, then a question waiting for
 * the person, then the latest task's state. An approval outranks the task because
 * the task is waiting on it, and the person has to see that first.
 */

export type HudTone = 'neutral' | 'active' | 'attention' | 'danger';

export type HudState =
  | { readonly kind: 'connecting' }
  | { readonly kind: 'offline' }
  | { readonly kind: 'approval'; readonly approval: PendingApproval }
  | { readonly kind: 'running'; readonly action: string | null }
  | { readonly kind: 'paused' }
  | { readonly kind: 'stopped'; readonly failed: boolean }
  | { readonly kind: 'idle' };

/** The latest task's status, from its most recent `task.status` event. */
function latestTaskStatus(snapshot: StreamSnapshot): string | null {
  for (let i = snapshot.events.length - 1; i >= 0; i -= 1) {
    const event = snapshot.events[i];
    if (event?.type === 'task.status') {
      const status = event.payload['status'];
      return typeof status === 'string' ? status : null;
    }
  }
  return null;
}

/**
 * The current action in plain English, from the latest step event that carries one.
 * The step payloads are `P4`'s; until then a running task says only that it is working.
 */
function latestAction(snapshot: StreamSnapshot): string | null {
  for (let i = snapshot.events.length - 1; i >= 0; i -= 1) {
    const event = snapshot.events[i];
    if (event?.type === 'step.action' || event?.type === 'step.started') {
      const summary = event.payload['summary'];
      return typeof summary === 'string' && summary.length > 0 ? summary : null;
    }
    if (event?.type === 'task.status') return null;
  }
  return null;
}

export function hudState(snapshot: StreamSnapshot): HudState {
  if (snapshot.connection === null || snapshot.connection === 'connecting') {
    return snapshot.hasBeenLive ? { kind: 'offline' } : { kind: 'connecting' };
  }
  if (snapshot.connection !== 'live') return { kind: 'offline' };
  const approval = pendingApprovals(snapshot.events)[0];
  if (approval !== undefined) return { kind: 'approval', approval };
  switch (latestTaskStatus(snapshot)) {
    case 'RUNNING':
    case 'QUEUED':
      return { kind: 'running', action: latestAction(snapshot) };
    case 'PAUSED_BY_USER':
      return { kind: 'paused' };
    case 'STOPPED':
      return { kind: 'stopped', failed: false };
    case 'FAILED':
    case 'ABANDONED':
      return { kind: 'stopped', failed: true };
    default:
      return { kind: 'idle' };
  }
}

/** `UI.md § 6`: amber for a question or a pause, red for a stop or an error. */
export function hudTone(state: HudState): HudTone {
  switch (state.kind) {
    case 'running':
      return 'active';
    case 'approval':
    case 'paused':
      return 'attention';
    case 'offline':
    case 'stopped':
      return 'danger';
    default:
      return 'neutral';
  }
}

/** The one line of text, in plain English (`UI.md § 11`). */
export function hudText(state: HudState): string {
  switch (state.kind) {
    case 'connecting':
      return 'Connecting to the engine…';
    case 'offline':
      return 'The Aegis engine isn’t running.';
    case 'approval':
      return `Waiting for you: ${state.approval.prompt}`;
    case 'running':
      return state.action ?? 'Working…';
    case 'paused':
      return 'You took over — Aegis paused.';
    case 'stopped':
      return state.failed ? 'Stopped. The task did not finish.' : 'Stopped by you.';
    case 'idle':
      return 'Aegis is ready.';
  }
}

/** Whether Stop does anything here: there is a task or a question to stop. */
export function canStop(state: HudState): boolean {
  return state.kind === 'running' || state.kind === 'paused' || state.kind === 'approval';
}
