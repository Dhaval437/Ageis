import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CoreRequest, CoreResponse } from '@aegis/shared';
import type { CoreGateway } from '../src/main/bridge-handlers.js';
import {
  MISSES_TO_KILL,
  PING_INTERVAL_MS,
  PING_TIMEOUT_MS,
  createWatchdog,
  type WatchdogOptions,
} from '../src/main/watchdog.js';

type Answer = 'ok' | 'hang' | 'refuse' | number;

/** A core whose next answers are scripted; the last one repeats. */
function scriptedGateway(...answers: Answer[]): CoreGateway & { calls: CoreRequest[] } {
  const calls: CoreRequest[] = [];
  return {
    calls,
    request: (request) => {
      calls.push(request);
      const answer = answers.length > 1 ? answers.shift() : answers[0];
      if (answer === 'hang') return new Promise<CoreResponse>(() => undefined);
      if (answer === 'refuse') return Promise.reject(new Error('Aegis could not reach its core.'));
      const status = answer === 'ok' || answer === undefined ? 200 : answer;
      return Promise.resolve({ status, body: { status: 'ok' } });
    },
  };
}

/**
 * Fake timers run a 0 ms timeout 1 ms later, so each back-to-back round drifts
 * by a millisecond. Far below anything the watchdog decides on.
 */
const SLACK_MS = 10;

/** Wall-clock skew added to `now()`, to stand in for a machine that slept. */
let skew = 0;

function build(overrides: Partial<WatchdogOptions> & { gateway: () => CoreGateway | null }) {
  const terminate = vi.fn(() => Promise.resolve(true));
  const onReport = vi.fn();
  const watchdog = createWatchdog({
    taskRunning: () => true,
    terminate,
    onReport,
    now: () => Date.now() + skew,
    ...overrides,
  });
  return { watchdog, terminate, onReport };
}

beforeEach(() => {
  vi.useFakeTimers();
  skew = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('createWatchdog', () => {
  it('pings /health once a second while a task runs, and leaves a healthy core alone', async () => {
    const gateway = scriptedGateway('ok');
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);

    expect(gateway.calls).toHaveLength(10);
    expect(gateway.calls[0]).toEqual({ method: 'GET', path: '/health' });
    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('terminates a hung core after exactly two missed pings, and no sooner', async () => {
    const gateway = scriptedGateway('hang');
    // As the supervisor does: once terminated, there is no gateway until a new core.
    let alive = true;
    const terminate = vi.fn(() => {
      alive = false;
      return Promise.resolve(true);
    });
    const { watchdog, onReport } = build({ gateway: () => (alive ? gateway : null), terminate });
    watchdog.start();

    // First ping at 1 s, its deadline at 2 s; the second goes out at once and
    // misses at 3 s.
    const killAt = PING_INTERVAL_MS + MISSES_TO_KILL * PING_TIMEOUT_MS;
    await vi.advanceTimersByTimeAsync(killAt - 1);
    expect(terminate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(SLACK_MS);

    expect(terminate).toHaveBeenCalledOnce();
    expect(gateway.calls).toHaveLength(MISSES_TO_KILL);
    expect(onReport).toHaveBeenCalledWith({ outcome: 'terminated', misses: 2, silentMs: null });
    watchdog.stop();
  });

  it('reports how long the core had been silent', async () => {
    const gateway = scriptedGateway('ok', 'hang');
    const { watchdog, onReport } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + 3 * PING_TIMEOUT_MS + SLACK_MS);

    expect(onReport).toHaveBeenCalledOnce();
    const report = onReport.mock.calls[0]?.[0] as { silentMs: number };
    // The wait for the next ping, then two full deadlines.
    const silent = PING_INTERVAL_MS + 2 * PING_TIMEOUT_MS;
    expect(report.silentMs).toBeGreaterThanOrEqual(silent);
    expect(report.silentMs).toBeLessThan(silent + SLACK_MS);
    watchdog.stop();
  });

  it.each([
    ['a refused connection', 'refuse' as const],
    ['a 500', 500],
    ['a 401', 401],
  ])('counts %s as a miss', async (_why, answer) => {
    const gateway = scriptedGateway(answer);
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(2 * PING_INTERVAL_MS);

    expect(terminate).toHaveBeenCalledOnce();
    watchdog.stop();
  });

  it('forgives one miss once the core answers again', async () => {
    const gateway = scriptedGateway('hang', 'ok', 'hang', 'ok');
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);

    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('does not ping at all while no task is running', async () => {
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway, taskRunning: () => false });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);

    expect(gateway.calls).toHaveLength(0);
    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('does not kill a core whose task ended while it was being counted', async () => {
    let running = true;
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway, taskRunning: () => running });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + PING_TIMEOUT_MS + 1);
    running = false;
    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);

    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('does not kill a core whose task ended while the deciding ping was out', async () => {
    let running = true;
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway, taskRunning: () => running });
    watchdog.start();

    // The second ping is in flight; the task finishes before its deadline.
    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + 1.5 * PING_TIMEOUT_MS);
    running = false;
    await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS);

    expect(gateway.calls).toHaveLength(2);
    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('does nothing while there is no core', async () => {
    const { watchdog, terminate } = build({ gateway: () => null });
    watchdog.start();
    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);
    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('never adds a miss by one core to a miss by the next', async () => {
    const first = scriptedGateway('hang');
    const second = scriptedGateway('hang');
    let current: CoreGateway = first;
    const { watchdog, terminate } = build({ gateway: () => current });
    watchdog.start();

    // One miss against the first core, then the supervisor hands over a new one.
    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + PING_TIMEOUT_MS);
    current = second;
    // The new core's first miss: carried over, that would have been two.
    await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS + SLACK_MS);
    expect(terminate).not.toHaveBeenCalled();

    // Its own second miss, and the second core is judged.
    await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS);
    expect(terminate).toHaveBeenCalledOnce();
    expect(second.calls.length).toBeGreaterThanOrEqual(MISSES_TO_KILL);
    watchdog.stop();
  });

  it('does not blame the core for a round in which MAIN itself stopped (sleep)', async () => {
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    // Every deadline in this test fires ten seconds "late": the wall clock
    // jumped, as it does when a laptop wakes up.
    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS);
    for (let round = 0; round < 5; round += 1) {
      skew += 10_000;
      await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS);
    }

    expect(gateway.calls.length).toBeGreaterThanOrEqual(5);
    expect(terminate).not.toHaveBeenCalled();
    watchdog.stop();
  });

  it('still kills once MAIN is back on time', async () => {
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS);
    skew += 10_000;
    await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS);
    expect(terminate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2 * PING_TIMEOUT_MS + SLACK_MS);

    expect(terminate).toHaveBeenCalledOnce();
    watchdog.stop();
  });

  it('keeps watching after a terminate that throws', async () => {
    const gateway = scriptedGateway('hang');
    const terminate = vi.fn(() => Promise.reject(new Error('no such process')));
    const { watchdog, onReport } = build({ gateway: () => gateway, terminate });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + 4 * PING_TIMEOUT_MS + SLACK_MS);

    expect(terminate).toHaveBeenCalledTimes(2);
    expect(onReport).toHaveBeenCalledWith(expect.objectContaining({ outcome: 'failed' }));
    watchdog.stop();
  });

  it('acts on nothing after stop(), including a ping already in flight', async () => {
    const gateway = scriptedGateway('hang');
    const { watchdog, terminate } = build({ gateway: () => gateway });
    watchdog.start();

    await vi.advanceTimersByTimeAsync(PING_INTERVAL_MS + PING_TIMEOUT_MS + 1);
    watchdog.stop();
    await vi.advanceTimersByTimeAsync(10 * PING_INTERVAL_MS);

    expect(terminate).not.toHaveBeenCalled();
    expect(gateway.calls).toHaveLength(2);
  });

  it('starts once however often start() is called', async () => {
    const gateway = scriptedGateway('ok');
    const { watchdog } = build({ gateway: () => gateway });
    watchdog.start();
    watchdog.start();
    await vi.advanceTimersByTimeAsync(3 * PING_INTERVAL_MS);
    expect(gateway.calls).toHaveLength(3);
    watchdog.stop();
  });
});
