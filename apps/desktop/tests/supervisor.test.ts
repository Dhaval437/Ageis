import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { describe, expect, it, vi } from 'vitest';
import type { CoreGateway } from '../src/main/bridge-handlers.js';
import type { CoreLaunchSpec } from '../src/main/handshake.js';
import {
  createSupervisor,
  resolveCoreLaunch,
  type StartCoreFn,
  type SupervisorState,
} from '../src/main/supervisor.js';

/**
 * A stand-in for a live core: real streams (the supervisor drains them), and a
 * record of how it was killed, because "the core is gone" is the guarantee this
 * module exists to make.
 */
class FakeCore extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly stdin = new PassThrough();
  readonly kills: string[] = [];
  exitCode: number | null = null;
  signalCode: string | null = null;

  kill(signal?: string): boolean {
    this.kills.push(signal ?? 'SIGTERM');
    if (signal === 'SIGKILL' || this.kills.length === 1) this.exit(0);
    return true;
  }

  /** The core dying on its own — a crash, or someone else's `taskkill`. */
  exit(code: number): void {
    if (this.exitCode !== null) return;
    this.exitCode = code;
    this.emit('exit', code, null);
  }
}

/** A core that never dies when asked, to exercise the SIGKILL escalation. */
class StubbornCore extends FakeCore {
  override kill(signal?: string): boolean {
    this.kills.push(signal ?? 'SIGTERM');
    if (signal === 'SIGKILL') this.exit(137);
    return true;
  }
}

const SPEC: CoreLaunchSpec = { command: 'aegis-core.exe', args: ['--port', '0'] };

function healthyGateway(status = 200): CoreGateway {
  return { request: vi.fn(() => Promise.resolve({ status, body: { status: 'ok' } })) };
}

function unreachableGateway(): CoreGateway {
  return {
    request: vi.fn(() => Promise.reject(new Error('Aegis could not reach its core.'))),
  };
}

interface Harness {
  readonly cores: FakeCore[];
  readonly startCoreFn: StartCoreFn;
}

/** Hands out one fake core per spawn, so a respawn is visibly a new process. */
function spawner(make: (index: number) => FakeCore | Error): Harness {
  const cores: FakeCore[] = [];
  const startCoreFn: StartCoreFn = () => {
    const next = make(cores.length);
    if (next instanceof Error) return Promise.reject(next);
    cores.push(next);
    return Promise.resolve({
      child: next as never,
      session: {
        port: 49_000 + cores.length,
        pid: 4000 + cores.length,
        version: '0.0.0',
        // A distinct token per spawn, as a real handshake mints.
        token: String(cores.length).padStart(64, '0'),
      },
    });
  };
  return { cores, startCoreFn };
}

/** Waits for the supervisor to settle on one of the given statuses. */
async function settleOn(
  read: () => SupervisorState,
  statuses: readonly SupervisorState['status'][],
): Promise<SupervisorState> {
  for (let tick = 0; tick < 200; tick += 1) {
    if (statuses.includes(read().status)) return read();
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`supervisor stayed ${read().status}`);
}

describe('resolveCoreLaunch', () => {
  it('runs the PyInstaller bundle when packaged', () => {
    const spec = resolveCoreLaunch({
      isPackaged: true,
      resourcesPath: 'C:\\Program Files\\Aegis\\resources',
      projectRoot: 'C:\\repo',
    });
    expect(spec.command).toContain('aegis-core.exe');
    expect(spec.args).toEqual(['--port', '0']);
  });

  it('runs the venv interpreter from source in development', () => {
    const spec = resolveCoreLaunch({
      isPackaged: false,
      resourcesPath: 'C:\\unused',
      projectRoot: 'C:\\repo',
    });
    expect(spec.command).toContain('.venv');
    expect(spec.args).toEqual(['-m', 'aegis_core', '--port', '0']);
    expect(spec.cwd).toContain('core');
  });

  it('lets an explicit interpreter override both', () => {
    const spec = resolveCoreLaunch({
      isPackaged: true,
      resourcesPath: 'C:\\Program Files\\Aegis\\resources',
      projectRoot: 'C:\\repo',
      overrideCommand: 'C:\\python\\python.exe',
    });
    expect(spec.command).toBe('C:\\python\\python.exe');
  });

  it('never passes the port as anything but ephemeral', () => {
    for (const isPackaged of [true, false]) {
      const spec = resolveCoreLaunch({ isPackaged, resourcesPath: 'r', projectRoot: 'p' });
      expect(spec.args).toContain('0');
      expect(spec.args?.join(' ')).not.toMatch(/--token/);
    }
  });
});

describe('createSupervisor', () => {
  it('reports running once the core passes its health check', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
    });

    const state = await supervisor.start();

    expect(state.status).toBe('running');
    expect(supervisor.gateway()).not.toBeNull();
    expect(cores[0]?.kills).toEqual([]);
  });

  it('health-checks `/health`, and only accepts a 200', async () => {
    const gateway = healthyGateway();
    const { startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => gateway,
    });

    await supervisor.start();

    expect(gateway.request).toHaveBeenCalledWith({ method: 'GET', path: '/health' });
  });

  it('kills a core that answered the handshake but fails its health check', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: unreachableGateway,
      restartDelayMs: 1,
    });

    const state = await supervisor.start();

    // A core that does not answer is a core with mouse control and no purpose.
    expect(cores.every((core) => core.kills.length > 0)).toBe(true);
    expect(state.status).toBe('unavailable');
  });

  it('tries three times in the window, then gives up rather than spawning forever', async () => {
    let spawns = 0;
    const { cores, startCoreFn } = spawner(() => {
      spawns += 1;
      return new Error('spawn ENOENT');
    });
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
    });

    const state = await supervisor.start();

    expect(spawns).toBe(3);
    expect(cores).toHaveLength(0);
    expect(state).toMatchObject({ status: 'unavailable', attempts: 3 });
    expect(state.lastError).toContain('spawn ENOENT');
  });

  it('restarts on request after giving up, with a fresh attempt budget', async () => {
    let spawns = 0;
    const { cores, startCoreFn } = spawner(() => {
      spawns += 1;
      // The first three attempts fail, as an antivirus-blocked core does; the
      // user then fixes it and presses Restart engine.
      return spawns <= 3 ? new Error('spawn ENOENT') : new FakeCore();
    });
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
      // Frozen clock: the 60 s window never expires on its own, so a restart
      // that works proves the budget was cleared and not merely waited out.
      now: () => 1_000,
    });

    expect((await supervisor.start()).status).toBe('unavailable');

    const state = await supervisor.restart();

    expect(state).toMatchObject({ status: 'running', attempts: 0, lastError: null });
    expect(cores).toHaveLength(1);
    await supervisor.stop();
  });

  it('kills the core that is there before starting another one', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
    });
    await supervisor.start();

    expect((await supervisor.restart()).status).toBe('running');

    expect(cores).toHaveLength(2);
    expect(cores[0]?.kills.length).toBeGreaterThan(0);
    expect(cores[1]?.exitCode).toBeNull();
    await supervisor.stop();
  });

  it('joins a restart already in flight rather than spawning twice', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
    });
    await supervisor.start();

    const [first, second] = await Promise.all([supervisor.restart(), supervisor.restart()]);

    expect(first).toEqual(second);
    expect(cores).toHaveLength(2);
    await supervisor.stop();
  });

  it('respawns a core that dies on its own', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
    });
    await supervisor.start();

    cores[0]?.exit(1);
    await settleOn(supervisor.state, ['running']);

    expect(cores).toHaveLength(2);
    expect(supervisor.gateway()).not.toBeNull();
  });

  it('has no gateway while the core is down', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 50,
    });
    await supervisor.start();

    cores[0]?.exit(1);

    // The renderer must see "the core is not there" rather than a request that
    // goes to a dead process.
    expect(supervisor.gateway()).toBeNull();
    expect(supervisor.state().status).toBe('restarting');
    await supervisor.stop();
  });

  it('starts a fresh session token per spawn', async () => {
    const tokens: string[] = [];
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: (options) => {
        tokens.push(options.token);
        return healthyGateway();
      },
      restartDelayMs: 1,
    });
    await supervisor.start();
    cores[0]?.exit(1);
    await settleOn(supervisor.state, ['running']);

    expect(tokens).toHaveLength(2);
    await supervisor.stop();
  });

  it('reports each healthy session, and its loss, to onSession', async () => {
    const sessions: ({ port: number; token: string } | null)[] = [];
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
      onSession: (session) => sessions.push(session),
    });
    await supervisor.start();
    cores[0]?.exit(1);
    await settleOn(supervisor.state, ['running']);
    await supervisor.stop();

    // Healthy core → gone → a new core with a new token → gone on stop.
    expect(sessions).toHaveLength(4);
    expect(sessions[0]).toEqual({ port: 49_001, token: '1'.padStart(64, '0') });
    expect(sessions[1]).toBeNull();
    expect(sessions[2]).toEqual({ port: 49_002, token: '2'.padStart(64, '0') });
    expect(sessions[3]).toBeNull();
  });

  it('never reports a session for a core that failed its health check', async () => {
    const onSession = vi.fn();
    const { startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(503),
      restartDelayMs: 1,
      onSession,
    });
    await supervisor.start();
    expect(supervisor.state().status).toBe('unavailable');
    expect(onSession).not.toHaveBeenCalled();
  });

  it('kills the core on stop and does not restart it', async () => {
    const { cores, startCoreFn } = spawner(() => new FakeCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
    });
    await supervisor.start();

    await supervisor.stop();

    expect(cores[0]?.kills).toEqual(['SIGTERM']);
    expect(supervisor.state().status).toBe('stopped');
    expect(supervisor.gateway()).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(cores).toHaveLength(1);
  });

  it('escalates to SIGKILL when the core ignores the polite kill', async () => {
    const { cores, startCoreFn } = spawner(() => new StubbornCore());
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
    });
    await supervisor.start();

    await supervisor.stop();

    expect(cores[0]?.kills).toEqual(['SIGTERM', 'SIGKILL']);
  }, 10_000);

  it('kills a core that finished starting after stop was called', async () => {
    const core = new FakeCore();
    let release = (): void => {};
    const spawning = new Promise<void>((resolve) => {
      release = resolve;
    });
    const startCoreFn: StartCoreFn = async () => {
      await spawning;
      return {
        child: core as never,
        session: { port: 49_001, pid: 4001, version: '0.0.0', token: 'b'.repeat(64) },
      };
    };
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
    });

    const starting = supervisor.start();
    await supervisor.stop();
    release();
    await starting;

    // Invariant 14 again: a core that arrives during shutdown is still a core.
    expect(core.kills).toEqual(['SIGTERM']);
    expect(supervisor.gateway()).toBeNull();
  });

  it('forgets attempts that fall outside the 60 s window', async () => {
    let clock = 0;
    let failures = 3;
    const { startCoreFn } = spawner(() =>
      failures-- > 0 ? new Error('spawn ENOENT') : new FakeCore(),
    );
    const supervisor = createSupervisor({
      spec: SPEC,
      startCoreFn,
      createGatewayFn: () => healthyGateway(),
      restartDelayMs: 1,
      now: () => clock,
    });

    expect((await supervisor.start()).status).toBe('unavailable');
    clock += 61_000;
    const state = await supervisor.start();

    expect(state.status).toBe('running');
    await supervisor.stop();
  });
  describe('terminate (the kill switch, P3-06)', () => {
    /** The development shape: MAIN's child is a launcher, the core its child. */
    class LauncherCore extends FakeCore {
      constructor(readonly pid: number) {
        super();
      }
    }

    function build(make: (index: number) => FakeCore) {
      const terminated: number[] = [];
      const harness = spawner(make);
      const sessions: (number | null)[] = [];
      const supervisor = createSupervisor({
        spec: SPEC,
        startCoreFn: harness.startCoreFn,
        createGatewayFn: () => healthyGateway(),
        restartDelayMs: 1,
        terminatePid: (pid) => terminated.push(pid),
        onSession: (session) => sessions.push(session?.port ?? null),
      });
      return { supervisor, terminated, sessions, ...harness };
    }

    it('terminates the handshake PID and the launcher at once, with no polite signal', async () => {
      const { supervisor, terminated, cores } = build(() => new LauncherCore(7000));
      await supervisor.start();

      expect(await supervisor.terminate('kill-switch')).toBe(true);

      // 4001 is the core that answered the handshake; 7000 the child MAIN spawned.
      expect(terminated).toEqual([4001, 7000]);
      expect(cores[0]?.kills).toEqual(['SIGKILL']);
      expect(supervisor.gateway()).toBeNull();
      await supervisor.stop();
    });

    it('terminates once when the child is the core, as when packaged', async () => {
      const { supervisor, terminated } = build(() => new LauncherCore(4001));
      await supervisor.start();
      await supervisor.terminate('kill-switch');
      expect(terminated).toEqual([4001]);
      await supervisor.stop();
    });

    it('starts a fresh core with a new session and a clean attempt budget', async () => {
      const { supervisor, sessions, cores } = build(() => new FakeCore());
      await supervisor.start();
      await supervisor.terminate('kill-switch');

      const state = await settleOn(() => supervisor.state(), ['running']);
      expect(cores).toHaveLength(2);
      expect(sessions).toEqual([49_001, null, 49_002]);
      expect(state.attempts).toBe(0);
      await supervisor.stop();
    });

    it('does not count its own kill as a crash', async () => {
      const { supervisor, cores } = build(() => new FakeCore());
      await supervisor.start();
      await supervisor.terminate('kill-switch');
      // The terminated process's exit arrives after MAIN has let go of it.
      cores[0]?.exit(1);
      await settleOn(() => supervisor.state(), ['running']);
      expect(supervisor.state().lastError).toBeNull();
      expect(cores).toHaveLength(2);
      await supervisor.stop();
    });

    it('answers false, and terminates nothing, with no core running', async () => {
      const { supervisor, terminated } = build(() => new FakeCore());
      expect(await supervisor.terminate('kill-switch')).toBe(false);
      await supervisor.start();
      await supervisor.stop();
      expect(await supervisor.terminate('kill-switch')).toBe(false);
      expect(terminated).toEqual([]);
    });

    it('counts a watchdog kill against the budget, unlike a kill-switch press', async () => {
      const { supervisor, cores } = build(() => new FakeCore());
      await supervisor.start();

      expect(await supervisor.terminate('watchdog')).toBe(true);
      const state = await settleOn(() => supervisor.state(), ['running']);

      expect(cores).toHaveLength(2);
      expect(state.attempts).toBe(1);
      expect(state.lastError).toBeNull();
      await supervisor.stop();
    });

    it('ends on the Engine-unavailable screen for a core that hangs on every task', async () => {
      const { supervisor, cores } = build(() => new FakeCore());
      await supervisor.start();

      // The first start plus two respawns is the whole budget; the third hang
      // leaves nothing to spend, so no fourth core is started.
      for (let hang = 0; hang < 3; hang += 1) {
        await settleOn(() => supervisor.state(), ['running']);
        await supervisor.terminate('watchdog');
      }
      const state = await settleOn(() => supervisor.state(), ['unavailable']);

      expect(cores).toHaveLength(3);
      expect(state.lastError).toBe('The engine stopped responding during a task, so Aegis stopped it.');
      expect(supervisor.gateway()).toBeNull();
      await supervisor.stop();
    });

    it('never terminates a PID MAIN has already seen exit', async () => {
      const { supervisor, terminated, cores } = build(() => new LauncherCore(7000));
      await supervisor.start();
      cores[0]?.exit(1);
      expect(await supervisor.terminate('kill-switch')).toBe(false);
      expect(terminated).toEqual([]);
      await supervisor.stop();
    });
  });
});
