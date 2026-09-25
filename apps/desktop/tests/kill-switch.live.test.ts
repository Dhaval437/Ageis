/**
 * The kill switch against real processes (`REVIEW.md § 5`: "kill switch still
 * stops everything with the core hung").
 *
 * The real supervisor, the real handshake, the real gateway and the real
 * `TerminateProcess`, against `fixtures/fake-core.mts` — a launcher and a core
 * beneath it, the development shape. `hung` wedges the core's event loop the
 * moment it is asked to stop, so nothing in it can answer or clean up: the
 * only way it stops is MAIN killing it.
 */

import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { startCore } from '../src/main/handshake.js';
import { createKillSwitch } from '../src/main/kill-switch.js';
import { createSupervisor, type Supervisor } from '../src/main/supervisor.js';

const FIXTURE = fileURLToPath(new URL('./fixtures/fake-core.mts', import.meta.url));

/** `ARCHITECTURE.md § 13`: kill switch → all input released. */
const BUDGET_MS = 200;

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
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  return check();
}

let supervisor: Supervisor | null = null;

afterEach(async () => {
  await supervisor?.stop();
  supervisor = null;
});

async function startFakeCore(mode: 'healthy' | 'hung') {
  const pids: { launcher: number; core: number }[] = [];
  supervisor = createSupervisor({
    spec: { command: process.execPath, args: [FIXTURE, 'launcher', mode] },
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
  // The development shape this test exists for: the core is a grandchild.
  expect(first.core).not.toBe(first.launcher);
  const killSwitch = createKillSwitch({
    gateway: () => running.gateway(),
    terminate: () => running.terminate('kill-switch'),
  });
  return { running, killSwitch, ...first };
}

describe('the kill switch, live', () => {
  it(`stops a wedged core inside ${String(BUDGET_MS)} ms and leaves neither process`, async () => {
    const { running, killSwitch, launcher, core } = await startFakeCore('hung');

    const pressed = performance.now();
    const report = await killSwitch.trigger();
    const coreGone = await until(() => !alive(core), 1_000);
    const stoppedMs = performance.now() - pressed;

    expect(report.outcome).toBe('terminated');
    expect(coreGone).toBe(true);
    expect(stoppedMs).toBeLessThan(BUDGET_MS);
    expect(await until(() => !alive(launcher), 1_000)).toBe(true);
    expect(running.gateway()).toBeNull();
  });

  it('leaves a healthy core running once it acknowledges', async () => {
    const { running, killSwitch, core } = await startFakeCore('healthy');

    const report = await killSwitch.trigger();

    expect(report.outcome).toBe('acknowledged');
    expect(report.elapsedMs).toBeLessThan(BUDGET_MS);
    expect(alive(core)).toBe(true);
    expect(running.gateway()).not.toBeNull();
  });
});
