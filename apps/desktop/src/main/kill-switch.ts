/**
 * What the kill switch does when it fires (`P3-06`, `ARCHITECTURE.md § 8.3`).
 *
 * Two halves, and the second never waits on the first for long:
 *
 *  1. **Ask.** `POST /v1/kill`. A healthy core freezes, releases every key and
 *     button it holds, and answers — and stays running, so its journal, its
 *     timeline and the Undo offer survive the stop.
 *  2. **Terminate.** No `200` inside `KILL_ACK_TIMEOUT_MS` — the core is hung,
 *     wedged, gone or lying — and MAIN terminates it outright. A hung agent must
 *     never be a still-clicking agent, and a request is exactly what a hung core
 *     cannot answer (REMEMBER.md invariant 2).
 *
 * The budget is `ARCHITECTURE.md § 13`'s "kill switch → all input released
 * < 200 ms". Asking costs a loopback round trip on a healthy core; the deadline
 * plus a `TerminateProcess` bounds the hung case well inside it.
 *
 * What terminating does **not** do is release keys the dead core was holding:
 * Windows keeps an injected `KEYDOWN` down after the injector dies, and nothing
 * in MAIN can send a `KEYUP` yet. That is `P3-15`, on this same path.
 *
 * `electron`-free: the gateway and the supervisor are injected.
 */

import type { CoreGateway } from './bridge-handlers.js';

/** How long a core gets to acknowledge before it is terminated. */
export const KILL_ACK_TIMEOUT_MS = 100;

/**
 * - `acknowledged`: the core froze and released its keys itself.
 * - `terminated`: it did not answer in time, and MAIN killed it.
 * - `no-core`: there was nothing to stop.
 */
export type KillOutcome = 'acknowledged' | 'terminated' | 'no-core';

export interface KillReport {
  readonly outcome: KillOutcome;
  /** Milliseconds from `trigger()` to the outcome. */
  readonly elapsedMs: number;
  /** From the core's answer; non-zero means a key may still be held (`P3-15`). */
  readonly releaseFailures: number;
}

export interface KillSwitchOptions {
  /** The live core's gateway, or `null` while there isn't one. */
  readonly gateway: () => CoreGateway | null;
  /**
   * Terminate the core now. Resolves `true` if there was a core to terminate.
   * The supervisor's `terminate('kill-switch')`.
   */
  readonly terminate: () => Promise<boolean>;
  readonly ackTimeoutMs?: number;
  readonly onReport?: (report: KillReport) => void;
  readonly now?: () => number;
}

export interface KillSwitch {
  /** Stop the agent. A press while one is in flight joins it. */
  readonly trigger: () => Promise<KillReport>;
}

/** A `KillResponse` from `POST /v1/kill`, and nothing that merely looks like one. */
function acknowledgement(body: unknown): { releaseFailures: number } | null {
  if (typeof body !== 'object' || body === null) return null;
  const { engaged, release_failures: failures } = body as Record<string, unknown>;
  if (engaged !== true) return null;
  if (typeof failures !== 'number' || !Number.isInteger(failures) || failures < 0) return null;
  return { releaseFailures: failures };
}

/** Settles `null` after `ms`, so a gateway that ignores its own timeout still cannot hold us. */
function deadline(ms: number): { promise: Promise<null>; cancel: () => void } {
  let timer: NodeJS.Timeout | undefined;
  const promise = new Promise<null>((resolve) => {
    timer = setTimeout(() => {
      resolve(null);
    }, ms);
  });
  return {
    promise,
    cancel: () => {
      clearTimeout(timer);
    },
  };
}

export function createKillSwitch(options: KillSwitchOptions): KillSwitch {
  const ackTimeoutMs = options.ackTimeoutMs ?? KILL_ACK_TIMEOUT_MS;
  const now = options.now ?? ((): number => performance.now());
  let inFlight: Promise<KillReport> | null = null;

  async function ask(gateway: CoreGateway): Promise<{ releaseFailures: number } | null> {
    const timer = deadline(ackTimeoutMs);
    try {
      const response = await Promise.race([
        gateway.request({ method: 'POST', path: '/kill' }),
        timer.promise,
      ]);
      if (response === null || response.status !== 200) return null;
      return acknowledgement(response.body);
    } catch {
      // Unreachable, refused, timed out: all mean the same thing here.
      return null;
    } finally {
      timer.cancel();
    }
  }

  async function run(): Promise<KillReport> {
    const started = now();
    const gateway = options.gateway();
    const ack = gateway === null ? null : await ask(gateway);

    let report: KillReport;
    if (ack !== null) {
      report = {
        outcome: 'acknowledged',
        elapsedMs: now() - started,
        releaseFailures: ack.releaseFailures,
      };
    } else {
      const terminated = await options.terminate();
      report = {
        outcome: terminated ? 'terminated' : 'no-core',
        elapsedMs: now() - started,
        releaseFailures: 0,
      };
    }
    options.onReport?.(report);
    return report;
  }

  return {
    trigger: (): Promise<KillReport> => {
      if (inFlight !== null) return inFlight;
      const running = run().finally(() => {
        inFlight = null;
      });
      inFlight = running;
      return running;
    },
  };
}
