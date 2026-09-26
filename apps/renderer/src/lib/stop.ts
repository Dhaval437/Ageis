import type { KillSwitchStop, StreamEvent } from '@aegis/shared';
import type { StreamSnapshot } from '@/stores/stream';

/**
 * Whether the last kill-switch press is still the news (`UI.md § 7`, P3-14), and
 * how to say it.
 *
 * A stop stays current until a task-status event **newer than the stop** says a
 * task is going again. Newer by time, not by order: the stop outlives a stream
 * reset, and the replay after one re-delivers events from before the press, which
 * must not clear it.
 */

/** The task states in which something is going again after a stop. */
const GOING = new Set(['QUEUED', 'RUNNING', 'PAUSED_BY_USER', 'WAITING_APPROVAL']);

function isGoingAfter(event: StreamEvent, at: number): boolean {
  if (event.type !== 'task.status') return false;
  const status = event.payload['status'];
  return typeof status === 'string' && GOING.has(status) && Date.parse(event.ts) > at;
}

/** The last press, unless a task has started or resumed since. */
export function currentStop(
  snapshot: Pick<StreamSnapshot, 'stop' | 'events'>,
): KillSwitchStop | null {
  const { stop } = snapshot;
  if (stop === null) return null;
  const at = Date.parse(stop.at);
  return snapshot.events.some((event) => isGoingAfter(event, at)) ? null : stop;
}

/** One sentence on what the stop did, in plain English (`UI.md § 11`). */
export function stopDetail(stop: KillSwitchStop): string {
  const ms = `${String(Math.round(stop.elapsedMs))} ms`;
  switch (stop.outcome) {
    case 'acknowledged':
      return `Aegis stopped in ${ms} and let go of the keyboard and mouse.`;
    case 'terminated':
      return `The engine didn’t answer, so Aegis ended it after ${ms} and let go of any keys it held. A fresh engine is starting.`;
    case 'no-core':
      return 'Nothing was running, so there was nothing to stop.';
  }
}
