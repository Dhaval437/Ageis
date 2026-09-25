/**
 * The watchdog against real processes (`P3-07`): a core that hangs mid-task,
 * with nobody pressing anything, is terminated by MAIN.
 *
 * The real supervisor, handshake, gateway, `TerminateProcess` and watchdog, at
 * the watchdog's real settings (a ping a second, two misses), against
 * `fixtures/fake-core.mts` in the development shape: a launcher and the core
 * beneath it. `POST /v1/test/wedge` makes the core answer and then wedge its
 * own event loop, which is what a hung core looks like from MAIN.
 */

import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { startCore } from '../src/main/handshake.js';
import { createSupervisor, type Supervisor } from '../src/main/supervisor.js';
import {
  MISSES_TO_KILL,
  PING_INTERVAL_MS,
  PING_TIMEOUT_MS,
  createWatchdog,
  type Watchdog,
  type WatchdogReport,
} from '../src/main/watchdog.js';

const FIXTURE = fileURLToPath(new URL('./fixtures/fake-core.mts', import.meta.url));

/** The slowest detection: a wedge just after a ping, then a full interval and two deadlines. */
const WORST_CASE_MS = PING_INTERVAL_MS + MISSES_TO_KILL * PING_TIMEOUT_MS;

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function until(check: () => boolean, timeoutMs: number): Promise<boolean> {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (check()) return true;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return check();
}

let supervisor: Supervisor | null = null;
let watchdog: Watchdog | null = null;

afterEach(async () => {
  watchdog?.stop();
  watchdog = null;
  await supervisor?.stop();
  supervisor = null;
});

async function start(taskRunning: () => boolean) {
  const pids: { launcher: number; core: number }[] = [];
  const reports: WatchdogReport[] = [];
  supervisor = createSupervisor({
    spec: { command: process.execPath, args: [FIXTURE, 'launcher', 'healthy'] },
    startCoreFn: async (spec, options) => {
      const started = await startCore(spec, options);
      pids.push({ launcher: started.child.pid ?? -1, core: started.session.pid });
      return started;
    },
    // Long enough that the test sees the terminated core, not its replacement.
    restartDelayMs: 60_000,
  });
  const running = supervisor;
  expect((await running.start()).status).toBe('running');
  const first = pids[0];
  if (first === undefined) throw new Error('no core started');
  watchdog = createWatchdog({
    gateway: () => running.gateway(),
    taskRunning,
    terminate: () => running.terminate('watchdog'),
    onReport: (report) => reports.push(report),
  });
  watchdog.start();
  return { running, reports, ...first };
}

async function wedge(running: Supervisor): Promise<void> {
  const gateway = running.gateway();
  if (gateway === null) throw new Error('no core to wedge');
  expect((await gateway.request({ method: 'POST', path: '/test/wedge' })).status).toBe(200);
}

describe('the watchdog, live', () => {
  it('terminates a core that hangs mid-task, launcher and all', async () => {
    const { running, reports, launcher, core } = await start(() => true);
    // A healthy core is left alone for a few rounds first.
    await new Promise((resolve) => setTimeout(resolve, 2.5 * PING_INTERVAL_MS));
    expect(reports).toEqual([]);

    const wedgedAt = performance.now();
    await wedge(running);
    const coreGone = await until(() => !alive(core), WORST_CASE_MS + 1_000);
    const detectedMs = performance.now() - wedgedAt;

    console.info(`watchdog: wedged core dead ${detectedMs.toFixed(0)} ms after the wedge`);
    expect(coreGone).toBe(true);
    expect(detectedMs).toBeLessThan(WORST_CASE_MS + 500);
    expect(reports).toEqual([expect.objectContaining({ outcome: 'terminated', misses: 2 })]);
    expect(await until(() => !alive(launcher), 1_000)).toBe(true);
    expect(running.state()).toMatchObject({ status: 'restarting', attempts: 0 });
  }, 15_000);

  it('leaves a hung core alone while no task is running', async () => {
    const { running, reports, core } = await start(() => false);

    await wedge(running);
    await new Promise((resolve) => setTimeout(resolve, WORST_CASE_MS + 500));

    expect(reports).toEqual([]);
    expect(alive(core)).toBe(true);
    // A wedged core cannot run its own parent watch, so it is not left spinning.
    await running.terminate('kill-switch');
    expect(await until(() => !alive(core), 1_000)).toBe(true);
  }, 15_000);
});
