import { EVENT_TYPES, type CoreConnection, type EventType, type StreamEvent } from '@aegis/shared';

/**
 * Runtime checks for what arrives over `core.subscribe`.
 *
 * The bridge types say what MAIN *means* to send; nothing enforces it at the IPC
 * boundary, and the payloads carry text the agent scraped off the user's screen.
 * So nothing reaches the store without passing through here first.
 */

const EVENT_TYPE_SET: ReadonlySet<string> = new Set(EVENT_TYPES);
const CONNECTION_STATES: ReadonlySet<string> = new Set<CoreConnection>([
  'connecting',
  'live',
  'down',
  'unavailable',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A well-formed `§ 9.2` event, or `null`. */
export function parseStreamEvent(value: unknown): StreamEvent | null {
  if (!isRecord(value)) return null;
  const { seq, ts, task_id: taskId, type, payload } = value;
  if (typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq < 1) return null;
  if (typeof ts !== 'string') return null;
  if (taskId !== null && typeof taskId !== 'string') return null;
  if (typeof type !== 'string' || !EVENT_TYPE_SET.has(type)) return null;
  if (!isRecord(payload)) return null;
  return { seq, ts, task_id: taskId, type: type as EventType, payload };
}

/** What the store can act on. An unreadable message is `invalid`, never a guess. */
export type ParsedStreamMessage =
  | { readonly kind: 'event'; readonly event: StreamEvent }
  | { readonly kind: 'reset' }
  | { readonly kind: 'connection'; readonly state: CoreConnection }
  | { readonly kind: 'invalid' };

export function parseStreamMessage(value: unknown): ParsedStreamMessage {
  if (!isRecord(value)) return { kind: 'invalid' };
  switch (value['kind']) {
    case 'event': {
      const event = parseStreamEvent(value['event']);
      return event === null ? { kind: 'invalid' } : { kind: 'event', event };
    }
    case 'reset':
      return { kind: 'reset' };
    case 'connection': {
      const state = value['state'];
      return typeof state === 'string' && CONNECTION_STATES.has(state)
        ? { kind: 'connection', state: state as CoreConnection }
        : { kind: 'invalid' };
    }
    default:
      return { kind: 'invalid' };
  }
}
