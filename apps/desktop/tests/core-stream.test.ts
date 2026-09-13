import type { IncomingMessage } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WebSocketServer } from 'ws';
import type { CoreStreamMessage } from '@aegis/shared';
import {
  CLOSE_INVALID_SINCE,
  CLOSE_REPLAY_UNAVAILABLE,
  connectWithWs,
  createCoreStream,
  type ConnectFn,
  type StreamSocketHandlers,
} from '../src/main/core-stream.js';

const SESSION_A = { port: 50_001, token: 'a'.repeat(64) };
const SESSION_B = { port: 50_002, token: 'b'.repeat(64) };

interface FakeSocket {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly handlers: StreamSocketHandlers;
  closed: boolean;
}

/** Records every connection attempt; the test drives open/message/close by hand. */
function harness(): {
  sockets: FakeSocket[];
  messages: CoreStreamMessage[];
  connect: ConnectFn;
  last: () => FakeSocket;
  event: (seq: number) => string;
} {
  const sockets: FakeSocket[] = [];
  const messages: CoreStreamMessage[] = [];
  const connect: ConnectFn = (url, headers, handlers) => {
    const socket: FakeSocket = { url, headers, handlers, closed: false };
    sockets.push(socket);
    return {
      close: () => {
        socket.closed = true;
      },
    };
  };
  const last = (): FakeSocket => {
    const socket = sockets.at(-1);
    if (socket === undefined) throw new Error('no socket was opened');
    return socket;
  };
  const event = (seq: number): string =>
    JSON.stringify({
      seq,
      ts: '2026-09-13T00:00:00.000+00:00',
      task_id: null,
      type: 'log',
      payload: {},
    });
  return { sockets, messages, connect, last, event };
}

function make(h: ReturnType<typeof harness>) {
  return createCoreStream({
    deliver: (message) => h.messages.push(message),
    connect: h.connect,
    reconnectBaseMs: 1,
    reconnectMaxMs: 4,
  });
}

const seqsOf = (messages: CoreStreamMessage[]): number[] =>
  messages.flatMap((m) => (m.kind === 'event' ? [(m.event as { seq: number }).seq] : []));

const waitFor = async (condition: () => boolean): Promise<void> => {
  for (let tick = 0; tick < 200; tick += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  throw new Error('condition never held');
};

describe('createCoreStream', () => {
  it('connects to the loopback stream with the bearer token and no cursor', () => {
    const h = harness();
    make(h).setSession(SESSION_A);
    expect(h.last().url).toBe('ws://127.0.0.1:50001/v1/stream');
    expect(h.last().headers).toEqual({ authorization: `Bearer ${SESSION_A.token}` });
    expect(h.messages).toEqual([{ kind: 'connection', state: 'connecting' }]);
  });

  it('forwards events in order and reports live', () => {
    const h = harness();
    make(h).setSession(SESSION_A);
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(1));
    h.last().handlers.onMessage(h.event(2));
    expect(h.messages).toContainEqual({ kind: 'connection', state: 'live' });
    expect(seqsOf(h.messages)).toEqual([1, 2]);
  });

  it('reconnects to the same core with since=<last seq> and drops duplicates', async () => {
    const h = harness();
    make(h).setSession(SESSION_A);
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(1));
    h.last().handlers.onMessage(h.event(2));
    h.last().handlers.onClose(1006);

    await waitFor(() => h.sockets.length === 2);
    expect(h.last().url).toBe('ws://127.0.0.1:50001/v1/stream?since=2');
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(2));
    h.last().handlers.onMessage(h.event(3));

    expect(seqsOf(h.messages)).toEqual([1, 2, 3]);
    expect(h.messages.some((m) => m.kind === 'reset')).toBe(false);
  });

  it.each([CLOSE_REPLAY_UNAVAILABLE, CLOSE_INVALID_SINCE])(
    'on close %i, tells the renderer to reset and replays from scratch',
    async (code) => {
      const h = harness();
      make(h).setSession(SESSION_A);
      h.last().handlers.onOpen();
      h.last().handlers.onMessage(h.event(7));
      h.last().handlers.onClose(code);

      await waitFor(() => h.sockets.length === 2);
      expect(h.messages).toContainEqual({ kind: 'reset' });
      expect(h.last().url).toBe('ws://127.0.0.1:50001/v1/stream');
    },
  );

  it('drops the cursor and resets when a different core takes over', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(40));

    stream.setSession(null);
    expect(h.sockets[0]?.closed).toBe(true);
    stream.setSession(SESSION_B);

    // The new core's seq restarts at 1; a `since=40` would silently skip 1..40.
    expect(h.last().url).toBe('ws://127.0.0.1:50002/v1/stream');
    expect(h.messages.filter((m) => m.kind === 'reset')).toHaveLength(1);
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(1));
    expect(seqsOf(h.messages)).toEqual([40, 1]);
  });

  it('ignores the same session being reported twice', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    stream.setSession({ ...SESSION_A });
    expect(h.sockets).toHaveLength(1);
  });

  it('says down while the core restarts, and unavailable once the supervisor gives up', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    stream.setAvailability('down');
    stream.setSession(null);
    expect(h.messages.at(-1)).toEqual({ kind: 'connection', state: 'down' });
    stream.setAvailability('unavailable');
    expect(h.messages.at(-1)).toEqual({ kind: 'connection', state: 'unavailable' });
  });

  it('on restart (renderer reload), resets and replays everything retained', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    h.last().handlers.onOpen();
    h.last().handlers.onMessage(h.event(5));

    stream.restart();

    expect(h.sockets[0]?.closed).toBe(true);
    expect(h.last().url).toBe('ws://127.0.0.1:50001/v1/stream');
    const afterRestart = h.messages.slice(h.messages.findIndex((m) => m.kind === 'reset'));
    expect(afterRestart).toEqual([{ kind: 'reset' }, { kind: 'connection', state: 'connecting' }]);
  });

  it('drops a frame it cannot read without advancing the cursor', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const h = harness();
    make(h).setSession(SESSION_A);
    h.last().handlers.onMessage('{not json');
    h.last().handlers.onMessage(JSON.stringify({ seq: 'one' }));
    h.last().handlers.onMessage(h.event(1));
    expect(seqsOf(h.messages)).toEqual([1]);
    expect(errors).toHaveBeenCalledTimes(2);
    // Only a size is logged — the frame itself may carry anything.
    expect(String(errors.mock.calls[0]?.[0])).not.toContain('not json');
    errors.mockRestore();
  });

  it('ignores callbacks from a socket it already replaced', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    const stale = h.last();
    stream.setSession(SESSION_B);
    stale.handlers.onMessage(h.event(1));
    stale.handlers.onClose(1006);
    expect(seqsOf(h.messages)).toEqual([]);
    expect(h.sockets).toHaveLength(2);
  });

  it('stops reconnecting once disposed', async () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    h.last().handlers.onClose(1006);
    stream.dispose();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(h.sockets).toHaveLength(1);
  });

  it('closes an open socket on dispose', () => {
    const h = harness();
    const stream = make(h);
    stream.setSession(SESSION_A);
    stream.dispose();
    expect(h.last().closed).toBe(true);
  });
});

describe('connectWithWs (against a real ws server)', () => {
  let server: WebSocketServer | null = null;

  afterEach(async () => {
    await new Promise<void>((resolve) => {
      if (server === null) resolve();
      else server.close(() => resolve());
    });
    server = null;
  });

  async function listen(): Promise<{
    port: number;
    upgrades: IncomingMessage[];
    wss: WebSocketServer;
  }> {
    const upgrades: IncomingMessage[] = [];
    const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
    server = wss;
    wss.on('connection', (_socket, request) => upgrades.push(request));
    await new Promise<void>((resolve) => wss.once('listening', () => resolve()));
    return { port: (wss.address() as AddressInfo).port, upgrades, wss };
  }

  it('sends the token, never an Origin, and delivers text frames and the close code', async () => {
    const { port, upgrades, wss } = await listen();
    wss.on('connection', (socket) => {
      socket.send('{"seq":1}');
      setTimeout(() => socket.close(4410), 10);
    });

    const received: string[] = [];
    let closeCode: number | null = null;
    connectWithWs(
      `ws://127.0.0.1:${String(port)}/v1/stream`,
      { authorization: 'Bearer t0k3n' },
      {
        onOpen: () => undefined,
        onMessage: (text) => received.push(text),
        onClose: (code) => {
          closeCode = code;
        },
      },
    );

    await waitFor(() => closeCode !== null);
    expect(upgrades[0]?.headers['authorization']).toBe('Bearer t0k3n');
    expect(upgrades[0]?.headers['origin']).toBeUndefined();
    expect(received).toEqual(['{"seq":1}']);
    expect(closeCode).toBe(4410);
  });

  it('reports a refused upgrade as a close, without throwing', async () => {
    const onClose = vi.fn();
    // Nothing is listening on this port once the server is closed.
    const { port, wss } = await listen();
    await new Promise<void>((resolve) => wss.close(() => resolve()));
    server = null;
    connectWithWs(
      `ws://127.0.0.1:${String(port)}/v1/stream`,
      {},
      {
        onOpen: () => undefined,
        onMessage: () => undefined,
        onClose,
      },
    );
    await waitFor(() => onClose.mock.calls.length === 1);
  });

  it('does not report a close it initiated', async () => {
    const { port, upgrades } = await listen();
    const onClose = vi.fn();
    const socket = connectWithWs(
      `ws://127.0.0.1:${String(port)}/v1/stream`,
      {},
      {
        onOpen: () => undefined,
        onMessage: () => undefined,
        onClose,
      },
    );
    await waitFor(() => upgrades.length === 1);
    socket.close();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(onClose).not.toHaveBeenCalled();
  });
});
