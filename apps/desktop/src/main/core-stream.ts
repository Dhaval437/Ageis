/**
 * MAIN's connection to the core's event stream (`WS /v1/stream`,
 * `ARCHITECTURE.md § 9.2`), forwarded to the renderer as `CoreStreamMessage`s.
 *
 * MAIN holds the socket, not the renderer, for the same reason `core-gateway.ts`
 * holds the HTTP client: the session token never leaves MAIN, and the renderer
 * has no network access at all (`§ 3`).
 *
 * The rules this file exists to keep:
 *
 *  - **Never a stream with a hole.** MAIN tracks the last `seq` it forwarded and
 *    reconnects with `?since=` after a drop. When the core cannot honour that
 *    (close `4410`), or a *different* core is now running, the renderer is told
 *    to `reset` and the stream is replayed from scratch.
 *  - **`seq` restarts with every core process until P6-06**, so the cursor is
 *    dropped whenever the supervisor hands over a new session — a stale cursor
 *    can look valid to a new core, and the core has no way to tell.
 *  - **A renderer that (re)loads starts from scratch**, because whatever it had
 *    been told died with its page.
 *  - **No `Origin` header**, no token in any log line, a cap on frame size.
 *
 * `electron`-free: the socket is injected, so every reconnect path is testable.
 */

import WebSocket from 'ws';
import type { CoreConnection, CoreStreamMessage } from '@aegis/shared';

/** Loopback only, as in `core-gateway.ts`. */
const LOOPBACK = '127.0.0.1';

/** The first retry is quick; a core that keeps refusing is not hammered. */
const RECONNECT_BASE_MS = 250;
const RECONNECT_MAX_MS = 5_000;

/** Bounds a hung upgrade, so a wedged core shows up as `connecting`, not silence. */
const HANDSHAKE_TIMEOUT_MS = 10_000;

/** No `§ 9.2` event is anywhere near this. It stops a runaway frame growing MAIN's heap. */
const MAX_FRAME_BYTES = 4 * 1024 * 1024;

/** Close codes from `ARCHITECTURE.md § 9.2`. */
export const CLOSE_REPLAY_UNAVAILABLE = 4410;
export const CLOSE_INVALID_SINCE = 4400;
export const CLOSE_UNSUPPORTED_DATA = 1003;

/** The session this stream belongs to. A new token means a new core. */
export interface StreamSession {
  readonly port: number;
  readonly token: string;
}

/** What the core supervisor is doing, as far as the stream cares. */
export type CoreAvailability = 'running' | 'down' | 'unavailable';

export interface StreamSocketHandlers {
  readonly onOpen: () => void;
  readonly onMessage: (text: string) => void;
  /** Always called exactly once per socket, including after a failed upgrade. */
  readonly onClose: (code: number) => void;
}

export interface StreamSocket {
  /** Closes the socket. `onClose` must not be called for a socket closed this way. */
  readonly close: () => void;
}

export type ConnectFn = (
  url: string,
  headers: Readonly<Record<string, string>>,
  handlers: StreamSocketHandlers,
) => StreamSocket;

export interface CoreStreamOptions {
  /** Where messages go — `BridgeSender.coreEvent` in production. */
  readonly deliver: (message: CoreStreamMessage) => void;
  /** Injected in tests; defaults to a `ws` client. */
  readonly connect?: ConnectFn;
  readonly reconnectBaseMs?: number;
  readonly reconnectMaxMs?: number;
}

export interface CoreStream {
  /** The live core's session, or `null` while there is none. */
  readonly setSession: (session: StreamSession | null) => void;
  /** The supervisor's state, so the renderer can tell "restarting" from "gave up". */
  readonly setAvailability: (availability: CoreAvailability) => void;
  /** The renderer (re)loaded: tell it to reset and replay everything retained. */
  readonly restart: () => void;
  /** Close the socket and stop reconnecting. Safe to call more than once. */
  readonly dispose: () => void;
}

/** The default socket: `ws`, because Electron 32's Node has no usable `WebSocket`. */
export const connectWithWs: ConnectFn = (url, headers, handlers) => {
  const socket = new WebSocket(url, {
    headers: { ...headers },
    handshakeTimeout: HANDSHAKE_TIMEOUT_MS,
    maxPayload: MAX_FRAME_BYTES,
    perMessageDeflate: false,
    followRedirects: false,
    // `origin` is deliberately not set: the core refuses any upgrade that has one.
  });
  let closedByUs = false;
  socket.on('open', handlers.onOpen);
  socket.on('message', (data, isBinary) => {
    if (isBinary) return;
    const bytes = Array.isArray(data)
      ? Buffer.concat(data)
      : Buffer.isBuffer(data)
        ? data
        : Buffer.from(data);
    handlers.onMessage(bytes.toString('utf8'));
  });
  // `ws` emits `error` before `close` on a refused upgrade or a reset; the close
  // handler does the work. The listener must exist or `ws` throws.
  socket.on('error', () => undefined);
  socket.on('close', (code) => {
    if (!closedByUs) handlers.onClose(code);
  });
  return {
    close: () => {
      closedByUs = true;
      socket.removeAllListeners('open');
      socket.removeAllListeners('message');
      if (socket.readyState === WebSocket.CONNECTING) socket.terminate();
      else socket.close(1000);
    },
  };
};

/** Pulls `seq` off a frame without trusting the rest of it; the renderer validates. */
function parseFrame(text: string): { seq: number; event: unknown } | null {
  let event: unknown;
  try {
    event = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof event !== 'object' || event === null) return null;
  const seq = (event as { seq?: unknown }).seq;
  if (typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq < 1) return null;
  return { seq, event };
}

export function createCoreStream(options: CoreStreamOptions): CoreStream {
  const connect = options.connect ?? connectWithWs;
  const baseMs = options.reconnectBaseMs ?? RECONNECT_BASE_MS;
  const maxMs = options.reconnectMaxMs ?? RECONNECT_MAX_MS;

  let session: StreamSession | null = null;
  let availability: CoreAvailability = 'down';
  let socket: StreamSocket | null = null;
  /** The last `seq` forwarded to the renderer for this session, or `null` for none. */
  let lastSeq: number | null = null;
  let retryTimer: NodeJS.Timeout | null = null;
  let failures = 0;
  let disposed = false;
  let connection: CoreConnection | null = null;

  function announce(state: CoreConnection): void {
    if (state === connection) return;
    connection = state;
    options.deliver({ kind: 'connection', state });
  }

  function reset(): void {
    lastSeq = null;
    options.deliver({ kind: 'reset' });
  }

  function clearRetry(): void {
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  function closeSocket(): void {
    clearRetry();
    const current = socket;
    socket = null;
    current?.close();
  }

  function open(): void {
    if (disposed || session === null || socket !== null) return;
    announce('connecting');
    const { port, token } = session;
    const since = lastSeq === null ? '' : `?since=${String(lastSeq)}`;
    const owner = session;
    const opened = connect(
      `ws://${LOOPBACK}:${String(port)}/v1/stream${since}`,
      { authorization: `Bearer ${token}` },
      {
        onOpen: () => {
          if (socket !== opened) return;
          failures = 0;
          announce('live');
        },
        onMessage: (text) => {
          if (socket !== opened) return;
          const frame = parseFrame(text);
          if (frame === null) {
            // Our own core validated this before sending it, so this is a bug —
            // but the frame may hold anything, so only its size is logged.
            console.error(
              `[main] dropped an unreadable stream frame (${String(text.length)} chars)`,
            );
            return;
          }
          // A duplicate across a reconnect boundary is harmless; applying it twice is not.
          if (lastSeq !== null && frame.seq <= lastSeq) return;
          lastSeq = frame.seq;
          options.deliver({ kind: 'event', event: frame.event });
        },
        onClose: (code) => {
          if (socket !== opened) return;
          socket = null;
          onClosed(code, owner);
        },
      },
    );
    socket = opened;
  }

  function onClosed(code: number, owner: StreamSession): void {
    if (disposed || session !== owner) return;
    if (
      code === CLOSE_REPLAY_UNAVAILABLE ||
      code === CLOSE_INVALID_SINCE ||
      code === CLOSE_UNSUPPORTED_DATA
    ) {
      // The cursor is unusable (or the core thinks we are broken): start over
      // rather than retrying the same request forever.
      reset();
    }
    failures += 1;
    announce(availability === 'running' ? 'connecting' : availabilityState());
    const delay = Math.min(maxMs, baseMs * 2 ** Math.min(failures - 1, 10));
    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, delay);
    retryTimer.unref?.();
  }

  function availabilityState(): CoreConnection {
    return availability === 'unavailable' ? 'unavailable' : 'down';
  }

  return {
    setSession: (next) => {
      if (disposed) return;
      if (next === null) {
        closeSocket();
        session = null;
        announce(availabilityState());
        return;
      }
      if (session !== null && session.token === next.token && session.port === next.port) return;
      closeSocket();
      const hadSession = session !== null || lastSeq !== null;
      session = next;
      failures = 0;
      // A different core: its `seq` has nothing to do with ours (until P6-06).
      if (hadSession) reset();
      lastSeq = null;
      open();
    },
    setAvailability: (next) => {
      if (disposed) return;
      availability = next;
      if (session === null) announce(availabilityState());
    },
    restart: () => {
      if (disposed) return;
      closeSocket();
      failures = 0;
      reset();
      // The page that heard the last announcement is gone; say it again.
      connection = null;
      if (session === null) announce(availabilityState());
      else open();
    },
    dispose: () => {
      disposed = true;
      closeSocket();
      session = null;
    },
  };
}
