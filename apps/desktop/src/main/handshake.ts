/**
 * MAIN's half of the startup handshake (ARCHITECTURE.md § 3.1, steps 1–3).
 *
 * MAIN mints a 256-bit session token, spawns the core, hands the token over a
 * **pipe on stdin**, and reads back one JSON line naming the ephemeral port the
 * core bound. Three things about that are load-bearing:
 *
 *  - **The token never touches argv.** `argv` is world-readable through WMI, so
 *    a token on the command line is a token every process on the machine has.
 *  - **The port is chosen by the OS, not by us.** A fixed port is a port malware
 *    can wait on; an ephemeral one has to be discovered, and the peer-PID check
 *    in the core makes discovering it insufficient anyway.
 *  - **stdout carries exactly one line.** Everything the core logs goes to
 *    stderr and its log file, so the reader below never has to guess which line
 *    is the handshake.
 *
 * `electron`-free on purpose, like `bridge-handlers.ts`: it is a child process
 * and a pipe, and it should be testable without an Electron process.
 *
 * **Not here:** the health check, the 3-strike respawn, killing the core when
 * MAIN exits, and the watchdog. Those are the supervisor's policy — P0-07 and
 * P3-07 — and this module is deliberately only the connection primitive.
 */

import { randomBytes } from 'node:crypto';
import { spawn as nodeSpawn, type ChildProcessWithoutNullStreams } from 'node:child_process';

/** 256 bits, per § 3.1 step 1, hex-encoded so it survives a text pipe intact. */
const TOKEN_BYTES = 32;

/** A cold start of a PyInstaller onedir bundle is slow; a hung one must still end. */
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 15_000;

/**
 * The handshake line is well under 200 bytes. The cap exists so a core that
 * floods stdout instead of answering cannot grow MAIN's heap without bound.
 */
const MAX_HANDSHAKE_BYTES = 8 * 1024;

/** Longer than any real version string, short enough to bound what we keep. */
const MAX_VERSION_CHARS = 64;

/**
 * MAIN tells the core which PID supervises it, rather than letting the core
 * read its own parent.
 *
 * A PID is not a secret, so argv is the right channel for it — and the core's
 * parent is *not* reliably MAIN: a launcher in the middle (a venv `python.exe`
 * re-executing, a stub `.exe`) makes MAIN a grandparent, and `getppid()` then
 * names a process the peer check would refuse MAIN for. Measured, not guessed.
 */
const SUPERVISOR_PID_FLAG = '--supervisor-pid';

export interface CoreLaunchSpec {
  /** The core executable, or the interpreter that runs it in development. */
  readonly command: string;
  /** `--supervisor-pid` is appended by `startCore`; do not pass it here. */
  readonly args?: readonly string[];
  readonly cwd?: string;
  readonly env?: NodeJS.ProcessEnv;
}

/** What the core announced on stdout, plus the credential MAIN gave it. */
export interface CoreSession {
  readonly port: number;
  readonly pid: number;
  readonly version: string;
  /** Never log this, never send it to the renderer (REMEMBER.md invariant 9). */
  readonly token: string;
}

export interface StartedCore {
  readonly child: ChildProcessWithoutNullStreams;
  readonly session: CoreSession;
}

export type SpawnFn = (spec: CoreLaunchSpec) => ChildProcessWithoutNullStreams;

export interface StartCoreOptions {
  readonly timeoutMs?: number;
  /** Injected in tests; defaults to a real `child_process.spawn`. */
  readonly spawn?: SpawnFn;
}

export class HandshakeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'HandshakeError';
  }
}

/** A fresh 256-bit token. One per core process, never reused across respawns. */
export function createSessionToken(): string {
  return randomBytes(TOKEN_BYTES).toString('hex');
}

/**
 * Parse the core's announcement.
 *
 * Strict on purpose: the line is the only thing MAIN believes about where the
 * core is, and a malformed one means the process on the other end is not the
 * core we meant to start.
 */
export function parseHandshakeLine(line: string): Omit<CoreSession, 'token'> | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null;
  const { port, pid, version } = parsed as Record<string, unknown>;

  if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) return null;
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid < 1) return null;
  if (typeof version !== 'string' || version.length === 0 || version.length > MAX_VERSION_CHARS) {
    return null;
  }
  return { port, pid, version };
}

function defaultSpawn(spec: CoreLaunchSpec): ChildProcessWithoutNullStreams {
  return nodeSpawn(spec.command, [...(spec.args ?? [])], {
    ...(spec.cwd === undefined ? {} : { cwd: spec.cwd }),
    ...(spec.env === undefined ? {} : { env: spec.env }),
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
}

/**
 * Spawn the core and complete the handshake.
 *
 * Resolves once the core has announced its port. On any failure — a spawn
 * error, a malformed line, an early exit, a timeout — the child is killed
 * before the rejection, so a failed handshake never leaves a process with mouse
 * control running unattended (REMEMBER.md invariant 14).
 */
export async function startCore(
  spec: CoreLaunchSpec,
  options: StartCoreOptions = {},
): Promise<StartedCore> {
  const spawn = options.spawn ?? defaultSpawn;
  const timeoutMs = options.timeoutMs ?? DEFAULT_HANDSHAKE_TIMEOUT_MS;
  const token = createSessionToken();

  const child = spawn({
    ...spec,
    args: [...(spec.args ?? []), SUPERVISOR_PID_FLAG, String(process.pid)],
  });

  return new Promise<StartedCore>((resolve, reject) => {
    let buffer = '';
    let settled = false;

    const timer = setTimeout(() => {
      fail(new HandshakeError('The core did not answer the startup handshake in time.'));
    }, timeoutMs);

    function cleanup(): void {
      clearTimeout(timer);
      child.stdout.off('data', onData);
      child.stdout.off('end', onEnd);
      child.off('error', onError);
      child.off('exit', onExit);
    }

    function fail(error: Error): void {
      if (settled) return;
      settled = true;
      cleanup();
      // The core may be alive but wedged. It has mouse control; it does not get
      // to stay running because its greeting was wrong.
      child.kill();
      reject(error);
    }

    function succeed(session: CoreSession): void {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ child, session });
    }

    function onData(chunk: Buffer | string): void {
      buffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
      const newline = buffer.indexOf('\n');
      if (newline === -1) {
        if (buffer.length > MAX_HANDSHAKE_BYTES) {
          fail(new HandshakeError('The core wrote too much before its handshake line.'));
        }
        return;
      }
      const announced = parseHandshakeLine(buffer.slice(0, newline));
      if (announced === null) {
        fail(new HandshakeError('The core sent a handshake line Aegis could not read.'));
        return;
      }
      succeed({ ...announced, token });
    }

    function onEnd(): void {
      fail(new HandshakeError('The core closed its output before announcing a port.'));
    }

    function onError(error: Error): void {
      fail(new HandshakeError(`The core could not be started: ${error.message}`));
    }

    function onExit(code: number | null, signal: string | null): void {
      fail(
        new HandshakeError(
          `The core exited during startup (${code === null ? `signal ${signal ?? 'unknown'}` : `code ${code}`}).`,
        ),
      );
    }

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', onData);
    child.stdout.once('end', onEnd);
    child.once('error', onError);
    child.once('exit', onExit);

    // Step 2: the token goes over the pipe, and the pipe is closed straight
    // after so the core's blocking `readline` returns instead of waiting.
    child.stdin.on('error', (error: Error) => {
      fail(new HandshakeError(`The core would not take the session token: ${error.message}`));
    });
    child.stdin.end(`${token}\n`, 'utf8');
  });
}
