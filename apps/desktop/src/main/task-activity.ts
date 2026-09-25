/**
 * Whether the core has a task **running**, as MAIN learns it from the event
 * stream it already forwards (`core-stream.ts`, `ARCHITECTURE.md § 9.2`).
 *
 * The watchdog (`watchdog.ts`, `P3-07`) acts only while this is true. It is
 * learnt from `task.status` events — pushed, never polled — so the answer
 * survives exactly the case it is needed for: a core that has stopped
 * answering still said, before it stopped, that a task was running.
 *
 * Only `RUNNING` counts. It is the one state in `RECOVERY.md § 3.1` in which
 * the agent moves the mouse; paused, waiting for an approval or queued, a
 * stalled core is not a clicking one.
 *
 * A `reset` clears everything, because it means a new core (whose tasks start
 * from nothing) or a replay from scratch that will say it all again.
 *
 * `electron`-free, like the rest of MAIN's logic.
 */

import { TASK_STATES, type CoreStreamMessage, type TaskState } from '@aegis/shared';

/**
 * Far more tasks than one core runs at once. A stream that claims more is
 * broken, and the set stays bounded (`REVIEW.md § 2`) — while still answering
 * `true`, which is the safe direction for the watchdog.
 */
const MAX_TRACKED = 64;

export interface TaskActivity {
  /** Feed every message MAIN forwards to the renderer, in order. */
  readonly observe: (message: CoreStreamMessage) => void;
  /** `true` while any task's last reported state is `RUNNING`. */
  readonly running: () => boolean;
}

function isTaskState(value: unknown): value is TaskState {
  return typeof value === 'string' && (TASK_STATES as readonly string[]).includes(value);
}

/** `{task_id, status}` from a `task.status` event, or `null` for anything else. */
function taskStatus(event: unknown): { taskId: string; status: TaskState } | null {
  if (typeof event !== 'object' || event === null) return null;
  const { type, task_id: taskId, payload } = event as Record<string, unknown>;
  if (type !== 'task.status' || typeof taskId !== 'string' || taskId.length === 0) return null;
  if (typeof payload !== 'object' || payload === null) return null;
  const { status } = payload as Record<string, unknown>;
  return isTaskState(status) ? { taskId, status } : null;
}

export function createTaskActivity(): TaskActivity {
  const running = new Set<string>();

  return {
    observe: (message) => {
      if (message.kind === 'reset') {
        running.clear();
        return;
      }
      if (message.kind !== 'event') return;
      const update = taskStatus(message.event);
      if (update === null) return;
      if (update.status !== 'RUNNING') {
        running.delete(update.taskId);
        return;
      }
      if (running.size < MAX_TRACKED) running.add(update.taskId);
    },
    running: () => running.size > 0,
  };
}
