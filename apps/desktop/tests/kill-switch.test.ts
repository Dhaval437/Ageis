import { describe, expect, it, vi } from 'vitest';
import type { CoreResponse } from '@aegis/shared';
import type { CoreGateway } from '../src/main/bridge-handlers.js';
import { KILL_ACK_TIMEOUT_MS, createKillSwitch } from '../src/main/kill-switch.js';

const ACK = { engaged: true, released_keys: 2, released_buttons: 0, release_failures: 0 };

function gatewayAnswering(response: Promise<CoreResponse>): CoreGateway {
  return { request: vi.fn(() => response) };
}

function hungGateway(): CoreGateway {
  return { request: vi.fn(() => new Promise<CoreResponse>(() => undefined)) };
}

function build(gateway: CoreGateway | null, options: { ackTimeoutMs?: number } = {}) {
  const terminate = vi.fn(() => Promise.resolve(true));
  const onReport = vi.fn();
  const killSwitch = createKillSwitch({
    gateway: () => gateway,
    terminate,
    onReport,
    ...options,
  });
  return { killSwitch, terminate, onReport };
}

describe('createKillSwitch', () => {
  it('asks the core first, and a healthy core is left running', async () => {
    const gateway = gatewayAnswering(Promise.resolve({ status: 200, body: ACK }));
    const { killSwitch, terminate, onReport } = build(gateway);

    const report = await killSwitch.trigger();

    expect(gateway.request).toHaveBeenCalledWith({ method: 'POST', path: '/kill' });
    expect(report.outcome).toBe('acknowledged');
    expect(report.releaseFailures).toBe(0);
    expect(terminate).not.toHaveBeenCalled();
    expect(onReport).toHaveBeenCalledWith(report);
  });

  it('passes on a core that could not release a key', async () => {
    const body = { ...ACK, release_failures: 1 };
    const { killSwitch } = build(gatewayAnswering(Promise.resolve({ status: 200, body })));
    expect((await killSwitch.trigger()).releaseFailures).toBe(1);
  });

  it('terminates a hung core once the deadline passes, and no later', async () => {
    vi.useFakeTimers();
    try {
      const { killSwitch, terminate } = build(hungGateway());
      const pending = killSwitch.trigger();

      await vi.advanceTimersByTimeAsync(KILL_ACK_TIMEOUT_MS - 1);
      expect(terminate).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);

      expect((await pending).outcome).toBe('terminated');
      expect(terminate).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the whole hung path inside the 200 ms budget in real time', async () => {
    const { killSwitch } = build(hungGateway());
    const report = await killSwitch.trigger();
    expect(report.outcome).toBe('terminated');
    expect(report.elapsedMs).toBeLessThan(200);
  });

  it.each<[string, () => Promise<CoreResponse>]>([
    ['unreachable', () => Promise.reject(new Error('Aegis could not reach its core.'))],
    ['a 500', () => Promise.resolve({ status: 500, body: null })],
    ['a 401', () => Promise.resolve({ status: 401, body: null })],
    [
      'a 200 that is not an acknowledgement',
      () => Promise.resolve({ status: 200, body: { ok: 1 } }),
    ],
    [
      'a 200 with engaged false',
      () => Promise.resolve({ status: 200, body: { ...ACK, engaged: false } }),
    ],
    [
      'a 200 with no failure count',
      () => Promise.resolve({ status: 200, body: { engaged: true } }),
    ],
    [
      'a 200 with a bad failure count',
      () => Promise.resolve({ status: 200, body: { ...ACK, release_failures: -1 } }),
    ],
    ['a 200 with no body', () => Promise.resolve({ status: 200, body: null })],
  ])('terminates a core whose answer is %s', async (_why, respond) => {
    const gateway: CoreGateway = { request: vi.fn(respond) };
    const { killSwitch, terminate } = build(gateway);
    expect((await killSwitch.trigger()).outcome).toBe('terminated');
    expect(terminate).toHaveBeenCalledOnce();
  });

  it('terminates without asking when there is no gateway', async () => {
    const { killSwitch, terminate } = build(null);
    expect((await killSwitch.trigger()).outcome).toBe('terminated');
    expect(terminate).toHaveBeenCalledOnce();
  });

  it('reports no-core when there was nothing to stop', async () => {
    const onReport = vi.fn();
    const killSwitch = createKillSwitch({
      gateway: () => null,
      terminate: () => Promise.resolve(false),
      onReport,
    });
    expect((await killSwitch.trigger()).outcome).toBe('no-core');
    expect(onReport).toHaveBeenCalledOnce();
  });

  it('a second press while one is in flight joins it rather than racing it', async () => {
    const gateway = hungGateway();
    const { killSwitch, terminate } = build(gateway, { ackTimeoutMs: 20 });
    const first = killSwitch.trigger();
    const second = killSwitch.trigger();
    expect(second).toBe(first);
    await first;
    expect(gateway.request).toHaveBeenCalledOnce();
    expect(terminate).toHaveBeenCalledOnce();

    // And once it has settled, the next press is a fresh stop.
    await killSwitch.trigger();
    expect(terminate).toHaveBeenCalledTimes(2);
  });
});

describe('the kill switch and MAIN’s own release (P3-15)', () => {
  function withRelease(body: unknown, respond = true) {
    const reasons: string[] = [];
    const gateway = respond
      ? gatewayAnswering(Promise.resolve({ status: 200, body }))
      : hungGateway();
    const killSwitch = createKillSwitch({
      gateway: () => gateway,
      terminate: () => Promise.resolve(true),
      releaseModifiers: (reason) => reasons.push(reason),
      ackTimeoutMs: 20,
    });
    return { killSwitch, reasons };
  }

  it('releases when a core acknowledged but could not release everything', async () => {
    const { killSwitch, reasons } = withRelease({ ...ACK, release_failures: 2 });
    await killSwitch.trigger();
    expect(reasons).toEqual(['the core could not release everything it held']);
  });

  it('leaves it to the core when the core released everything', async () => {
    const { killSwitch, reasons } = withRelease(ACK);
    await killSwitch.trigger();
    expect(reasons).toEqual([]);
  });

  it('leaves a terminated core to the supervisor, which releases after the kill', async () => {
    const { killSwitch, reasons } = withRelease(null, false);
    expect((await killSwitch.trigger()).outcome).toBe('terminated');
    expect(reasons).toEqual([]);
  });
});
