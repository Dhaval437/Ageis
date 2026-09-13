import { create } from 'zustand';
import type { CoreConnection, StreamEvent } from '@aegis/shared';
import { parseStreamMessage } from '@/lib/stream-event';

/**
 * The event-stream store. **The UI is a pure function of this** (REMEMBER.md
 * invariant 15): nothing in it is fetched, polled, or set by a component. Its
 * only writer is `apply`, fed by `connectStream` from `core.subscribe`.
 *
 * What it holds is deliberately generic until the task and step views need more
 * (P4-11): the connection state and the recent events in `seq` order. Views that
 * need per-task state derive it here, from events, when they land.
 */

/** Recent events kept for the views. `REVIEW.md § 2`: no unbounded buffer. */
export const MAX_EVENTS = 1000;

export interface StreamSnapshot {
  /** `null` until MAIN has said anything — the first paint, before the stream opens. */
  readonly connection: CoreConnection | null;
  /** Whether the stream has been `live` since the app started, for "Reconnecting…" copy. */
  readonly hasBeenLive: boolean;
  /** The last `seq` applied since the last reset; `0` for none. */
  readonly lastSeq: number;
  /** The most recent events, oldest first, at most `MAX_EVENTS`. */
  readonly events: readonly StreamEvent[];
  /** Messages that failed validation. Non-zero means a bug; never shown as data. */
  readonly rejected: number;
}

export const INITIAL_STREAM: StreamSnapshot = {
  connection: null,
  hasBeenLive: false,
  lastSeq: 0,
  events: [],
  rejected: 0,
};

/**
 * The whole store as one pure function, so every transition is testable without
 * React or a bridge.
 */
export function reduceStream(state: StreamSnapshot, message: unknown): StreamSnapshot {
  const parsed = parseStreamMessage(message);
  switch (parsed.kind) {
    case 'invalid':
      return { ...state, rejected: state.rejected + 1 };
    case 'reset':
      // Connection state is about MAIN's socket, not the stream's contents.
      return {
        ...INITIAL_STREAM,
        connection: state.connection,
        hasBeenLive: state.hasBeenLive,
        rejected: state.rejected,
      };
    case 'connection':
      return {
        ...state,
        connection: parsed.state,
        hasBeenLive: state.hasBeenLive || parsed.state === 'live',
      };
    case 'event': {
      // MAIN already drops duplicates; applying one twice would still be wrong here.
      if (parsed.event.seq <= state.lastSeq) return state;
      const events = [...state.events, parsed.event];
      return {
        ...state,
        lastSeq: parsed.event.seq,
        events: events.length > MAX_EVENTS ? events.slice(events.length - MAX_EVENTS) : events,
      };
    }
  }
}

export interface StreamStore extends StreamSnapshot {
  readonly apply: (message: unknown) => void;
}

export const useStreamStore = create<StreamStore>()((set) => ({
  ...INITIAL_STREAM,
  apply: (message) => {
    set((state) => reduceStream(state, message));
  },
}));
