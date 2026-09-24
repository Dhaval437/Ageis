/**
 * Spawns and supervises `aegis-core.exe`.
 *
 * `handshake.ts` is the connection primitive — one spawn, one token, one port.
 * This is the **lifecycle** around it (ARCHITECTURE.md § 3.1 steps 4 and 6,
 * RECOVERY.md § 4):
 *
 *  - health-check the core the moment it announces a port, because a process
 *    that started is not the same thing as a core that answers;
 *  - restart it when it dies or fails that check, at most 3 times in a 60 s
 *    window, and then stop and surface `unavailable` rather than spawn forever;
 *  - **kill the core whenever MAIN goes away.** A headless process with mouse
 *    control and no window is the failure mode we refuse to ship (REMEMBER.md
 *    invariant 14), so every MAIN exit path runs through `stop()`. The other
 *    half of that guarantee lives in the core itself
 *    (`aegis_core/parent_watch.py`), because a MAIN killed from Task Manager
 *    never gets to run any of this.
 *
 * `electron`-free on purpose, like `handshake.ts` and `bridge-handlers.ts`: a
 * state machine with timers is worth testing without an Electron process.
 */

import { join } from 'node:path';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { createCoreGateway } from './core-gateway.js';
import {
  startCore,
  type CoreLaunchSpec,
  type CoreSession,
  type StartCoreOptions,
} from './handshake.js';
import type { CoreGateway } from './bridge-handlers.js';

/** § 3.1 step 4: two failures in a row on `/v1/health` mean this core is unusable. */
const HEALTH_ATTEMPTS = 2;

/** The core has just answered the handshake; this only bounds a wedged one. */
const HEALTH_TIMEOUT_MS = 5_000;

/** RECOVERY.md § 4: at most 3 attempts inside this window, then the Recovery screen. */
const MAX_ATTEMPTS = 3;
const ATTEMPT_WINDOW_MS = 60_000;

/** Long enough not to spin on a crash loop, short enough to feel instant. */
const RESTART_DELAY_MS = 500;

/**
 * How long a core gets to die politely before it is killed outright. The core
 * has mouse control; it does not get to linger because it ignored a signal.
 */
const KILL_GRACE_MS = 1_500;

export type SupervisorStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  /** Down, with a restart already scheduled. */
  | 'restarting'
  /** Out of attempts. The renderer shows RECOVERY.md § 4's Engine-unavailable screen. */
  | 'unavailable';

export interface SupervisorState {
  readonly status: SupervisorStatus;
  /** Restarts used inside the current window, for the Recovery screen's copy. */
  readonly attempts: number;
  /** Why the last attempt failed, safe to show a user. Never carries the token. */
  readonly lastError: string | null;
}

export type StartCoreFn = (
  spec: CoreLaunchSpec,
  options?: StartCoreOptions,
) => Promise<{ child: ChildProcessWithoutNullStreams; session: CoreSession }>;

export type CreateGatewayFn = (options: { port: number; token: string }) => CoreGateway;

export interface SupervisorOptions {
  /** How to launch the core; see `resolveCoreLaunch`. */
  readonly spec: CoreLaunchSpec;
  /** Called on every state change, so MAIN can update the tray and the renderer. */
  readonly onState?: (state: SupervisorState) => void;
  /**
   * Called with the live core's port and token once it passes its health check,
   * and with `null` the moment it is gone. The event stream (`core-stream.ts`)
   * uses this to tell a new core from a reconnect to the same one.
   */
  readonly onSession?: (session: { port: number; token: string } | null) => void;
  /** Injected in tests. */
  readonly startCoreFn?: StartCoreFn;
  readonly createGatewayFn?: CreateGatewayFn;
  readonly restartDelayMs?: number;
  readonly healthTimeoutMs?: number;
  readonly now?: () => number;
  /** Injected in tests; defaults to `TerminateProcess` via `process.kill`. */
  readonly terminatePid?: (pid: number) => void;
}

export interface Supervisor {
  /** Starts the core and resolves once it is running, or out of attempts. */
  readonly start: () => Promise<SupervisorState>;
  /**
   * What `Restart engine` on the Engine-unavailable screen does (RECOVERY.md
   * § 4, P0-17): kill whatever core is there, **clear the attempt window**, and
   * start over. The window is cleared because a person asked — the 3-strike
   * rule exists to stop MAIN spawning forever on its own, not to make the app
   * unrecoverable until it is relaunched.
   */
  readonly restart: () => Promise<SupervisorState>;
  /** The gateway for the live core, or `null` while there isn't one. */
  readonly gateway: () => CoreGateway | null;
  readonly state: () => SupervisorState;
  /** Kills the core and stops restarting it. Safe to call more than once. */
  readonly stop: () => Promise<void>;
  /**
   * The kill switch's last resort (`P3-06`): terminate the core **now** — no
   * polite signal, no grace period — then start a fresh one with a clean
   * attempt budget. Resolves `true` if there was a running core to terminate.
   *
   * It terminates the process that answered the handshake as well as the
   * child MAIN spawned, because in development those differ: the venv's
   * `python.exe` re-execs, so the core holding the mouse is a *grandchild*.
   */
  readonly terminate: () => Promise<boolean>;
}

/**
 * Where the core is, in development and when packaged.
 *
 * Packaged, it is a PyInstaller **onedir** bundle under `resources/`
 * (REMEMBER.md § 4). In development there is no bundle, so the venv interpreter
 * runs the package from source — everything downstream sees the same spec, so
 * nothing else has to know which one it got.
 */
export interface CoreLocation {
  readonly isPackaged: boolean;
  /** `process.resourcesPath` when packaged. */
  readonly resourcesPath: string;
  /** The repo root in development. */
  readonly projectRoot: string;
  /** Overrides both, for pointing the app at an interpreter chosen by hand. */
  readonly overrideCommand?: string | undefined;
}

export function resolveCoreLaunch(location: CoreLocation): CoreLaunchSpec {
  if (location.overrideCommand !== undefined && location.overrideCommand.length > 0) {
    return {
      command: location.overrideCommand,
      args: ['-m', 'aegis_core', '--port', '0'],
      cwd: join(location.projectRoot, 'core'),
    };
  }
  if (location.isPackaged) {
    return {
      command: join(location.resourcesPath, 'core', 'aegis-core.exe'),
      args: ['--port', '0'],
    };
  }
  return {
    command: join(location.projectRoot, 'core', '.venv', 'Scripts', 'python.exe'),
    args: ['-m', 'aegis_core', '--port', '0'],
    cwd: join(location.projectRoot, 'core'),
  };
}

/** Kill the child, then make sure of it. Resolves when the process is gone. */
async function killChild(child: ChildProcessWithoutNullStreams, graceMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve) => {
    let done = false;
    const finish = (): void => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.off('exit', finish);
      resolve();
    };
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish();
    }, graceMs);
    timer.unref?.();
    child.once('exit', finish);
    child.kill();
  });
}

/** `TerminateProcess`. A process that is already gone is the outcome we wanted. */
function terminateProcess(pid: number): void {
  try {
    process.kill(pid, 'SIGKILL');
  } catch {
    // ESRCH: it died first.
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : 'The core failed to start.';
}

export function createSupervisor(options: SupervisorOptions): Supervisor {
  const startFn = options.startCoreFn ?? startCore;
  const makeGateway =
    options.createGatewayFn ??
    ((gatewayOptions: { port: number; token: string }) => createCoreGateway(gatewayOptions));
  const restartDelayMs = options.restartDelayMs ?? RESTART_DELAY_MS;
  const healthTimeoutMs = options.healthTimeoutMs ?? HEALTH_TIMEOUT_MS;
  const now = options.now ?? Date.now;
  const terminatePid = options.terminatePid ?? terminateProcess;

  let state: SupervisorState = { status: 'stopped', attempts: 0, lastError: null };
  let child: ChildProcessWithoutNullStreams | null = null;
  let gateway: CoreGateway | null = null;
  /** The PID the core reported in its handshake — the process that holds the mouse. */
  let corePid: number | null = null;
  let restartTimer: NodeJS.Timeout | null = null;
  /** Set by `stop()`, so an in-flight attempt cannot resurrect a stopped core. */
  let stopped = false;
  /** Timestamps of the attempts inside the current window. */
  let attemptTimes: number[] = [];
  let pending: Promise<SupervisorState> | null = null;

  function setState(patch: Partial<SupervisorState>): void {
    state = { ...state, ...patch };
    options.onState?.(state);
  }

  /** § 3.1 step 4: the core has to answer, not merely exist. */
  async function healthCheck(candidate: CoreGateway): Promise<void> {
    let last: Error = new Error('The core failed its health check.');
    for (let attempt = 0; attempt < HEALTH_ATTEMPTS; attempt += 1) {
      try {
        const response = await candidate.request({ method: 'GET', path: '/health' });
        if (response.status === 200) return;
        last = new Error(`The core answered its health check with ${String(response.status)}.`);
      } catch (error: unknown) {
        last = error instanceof Error ? error : last;
      }
    }
    throw last;
  }

  function scheduleRestart(): void {
    if (stopped || restartTimer !== null) return;
    setState({ status: 'restarting' });
    restartTimer = setTimeout(() => {
      restartTimer = null;
      void attemptLoop();
    }, restartDelayMs);
    restartTimer.unref?.();
  }

  function onCoreExit(exited: ChildProcessWithoutNullStreams): void {
    // A late exit from a core we already replaced or killed is not news.
    if (exited !== child) return;
    child = null;
    gateway = null;
    corePid = null;
    options.onSession?.(null);
    if (stopped) return;
    setState({ lastError: 'The core stopped unexpectedly.' });
    scheduleRestart();
  }

  /** One spawn + handshake + health check. Leaves the core running on success. */
  async function attemptOnce(): Promise<void> {
    const started = await startFn(options.spec, { timeoutMs: healthTimeoutMs * 3 });
    // The core logs to stderr and its own file; MAIN reads neither, but an
    // unread pipe fills and blocks the writer, so both are drained.
    started.child.stdout.resume();
    started.child.stderr.resume();

    if (stopped) {
      await killChild(started.child, KILL_GRACE_MS);
      throw new Error('Aegis is shutting down.');
    }
    const candidate = makeGateway({ port: started.session.port, token: started.session.token });
    try {
      await healthCheck(candidate);
    } catch (error: unknown) {
      await killChild(started.child, KILL_GRACE_MS);
      throw error;
    }
    child = started.child;
    gateway = candidate;
    corePid = started.session.pid;
    started.child.once('exit', () => {
      onCoreExit(started.child);
    });
    options.onSession?.({ port: started.session.port, token: started.session.token });
  }

  async function attemptLoop(): Promise<SupervisorState> {
    if (stopped) return state;
    if (pending !== null) return pending;

    const run = (async (): Promise<SupervisorState> => {
      while (!stopped) {
        const at = now();
        attemptTimes = [...attemptTimes.filter((time) => at - time < ATTEMPT_WINDOW_MS), at];
        if (attemptTimes.length > MAX_ATTEMPTS) {
          setState({ status: 'unavailable', attempts: MAX_ATTEMPTS });
          return state;
        }
        setState({ status: 'starting', attempts: attemptTimes.length - 1 });
        try {
          await attemptOnce();
          setState({ status: 'running', lastError: null });
          return state;
        } catch (error: unknown) {
          setState({ lastError: describe(error) });
        }
      }
      return state;
    })();

    pending = run;
    try {
      return await run;
    } finally {
      pending = null;
    }
  }

  async function stop(): Promise<void> {
    stopped = true;
    if (restartTimer !== null) {
      clearTimeout(restartTimer);
      restartTimer = null;
    }
    const running = child;
    child = null;
    gateway = null;
    corePid = null;
    if (running !== null) options.onSession?.(null);
    setState({ status: 'stopped' });
    if (running !== null) await killChild(running, KILL_GRACE_MS);
  }

  /** In flight, so a second click joins the first restart instead of racing it. */
  let restarting: Promise<SupervisorState> | null = null;

  function restart(): Promise<SupervisorState> {
    if (restarting !== null) return restarting;
    const run = (async (): Promise<SupervisorState> => {
      await stop();
      // A person asked for this one, so it starts from a clean budget. Without
      // this the spent window would make every restart fail instantly until the
      // 60 s elapsed, which reads as a dead button.
      attemptTimes = [];
      setState({ attempts: 0, lastError: null });
      stopped = false;
      return attemptLoop();
    })();
    restarting = run;
    return run.finally(() => {
      restarting = null;
    });
  }

  function terminate(): Promise<boolean> {
    const running = child;
    const pid = corePid;
    if (running === null) return Promise.resolve(false);
    // Forget it first, so its exit is not mistaken for a crash to count.
    child = null;
    gateway = null;
    corePid = null;
    options.onSession?.(null);
    // Only while `child` was still ours: a PID MAIN has seen exit may already
    // belong to somebody else's program.
    if (pid !== null) terminatePid(pid);
    if (running.pid !== undefined && running.pid !== pid) terminatePid(running.pid);
    // Belt and braces for the launcher; `TerminateProcess` again is harmless.
    running.kill('SIGKILL');
    setState({ lastError: 'The kill switch stopped the engine.' });
    // A person asked, as with `restart()`: a fresh core with no task in it is
    // what the stop leaves behind, and it must not be refused for a crash budget
    // the person did not spend.
    attemptTimes = [];
    setState({ attempts: 0 });
    scheduleRestart();
    return Promise.resolve(true);
  }

  return {
    terminate,
    start: (): Promise<SupervisorState> => {
      stopped = false;
      return attemptLoop();
    },
    restart,
    gateway: (): CoreGateway | null => gateway,
    state: (): SupervisorState => state,
    stop,
  };
}
