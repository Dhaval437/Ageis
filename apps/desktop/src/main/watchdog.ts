/**
 * The watchdog (`P3-07`, `ARCHITECTURE.md § 8.3`): MAIN pings the core once a
 * second while a task is running, and **two missed pings in a row terminate
 * it**. A hung agent must never be a still-clicking agent.
 *
 * The kill switch covers a hung core when a person notices; this covers it
 * when nobody does. A core whose event loop is wedged can still have an input
 * thread moving the mouse, and nothing inside that process can be trusted to
 * stop it — so MAIN terminates the process, the same way the kill switch's
 * last resort does, and the supervisor starts a fresh one.
 *
 * What counts as a miss is deliberately narrow:
 *
 *  - **A ping is `GET /v1/health` answered `200` inside `PING_TIMEOUT_MS`.**
 *    Anything else — a timeout, a refusal, another status — is a miss.
 *  - **Only while a task is `RUNNING`** (`task-activity.ts`). An idle core
 *    that stalls is not clicking anything, and killing it would throw away the
 *    event replay the renderer is about to ask for.
 *  - **Only when MAIN was on time.** If the ping's own deadline fired late,
 *    MAIN was the one frozen — the machine slept, or MAIN's event loop was
 *    blocked — and the silence says nothing about the core. Such a round is
 *    inconclusive and resets the count, so a laptop waking up never kills a
 *    healthy core.
 *  - **Against one core.** The count resets whenever the supervisor hands
 *    over a different gateway, so two misses are always two misses by the
 *    same process.
 *
 * `electron`-free: the gateway, the task signal and the terminate call are
 * injected, so every path runs under fake timers.
 */

import type { CoreGateway } from './bridge-handlers.js';

/** `ARCHITECTURE.md § 8.3`: "pings the core every second". */
export const PING_INTERVAL_MS = 1_000;

/**
 * How long one ping may take. `/v1/health` touches neither disk nor a model,
 * and a healthy core answers in milliseconds; a whole interval is generous.
 */
export const PING_TIMEOUT_MS = 1_000;

/** "Two missed pings while running → MAIN kills the core." */
export const MISSES_TO_KILL = 2;

/**
 * How late a deadline may fire before the round says more about MAIN than
 * about the core. Timers on a busy MAIN drift by milliseconds; a machine that
 * slept, or a MAIN blocked on something, drifts by far more than this.
 */
export const LATE_TOLERANCE_MS = 250;

export interface WatchdogReport {
  /**
   * `terminated` if a core was killed; `no-core` if it was already gone;
   * `failed` if terminating threw — the watchdog keeps running, and two more
   * misses try again.
   */
  readonly outcome: 'terminated' | 'no-core' | 'failed';
  /** Consecutive misses that triggered it. */
  readonly misses: number;
  /** Milliseconds since this core last answered a ping, or `null` if it never did. */
  readonly silentMs: number | null;
}

export interface WatchdogOptions {
  /** The live core's gateway, or `null` while there isn't one. */
  readonly gateway: () => CoreGateway | null;
  /** Whether a task is running right now (`TaskActivity.running`). */
  readonly taskRunning: () => boolean;
  /**
   * Terminate the core. The supervisor's `terminate('watchdog')`: counted as a
   * failure, unlike a kill-switch press, because nobody asked for it.
   */
  readonly terminate: () => Promise<boolean>;
  readonly onReport?: (report: WatchdogReport) => void;
  readonly intervalMs?: number;
  readonly timeoutMs?: number;
  readonly lateToleranceMs?: number;
  readonly now?: () => number;
}

export interface Watchdog {
  /** Starts pinging. Idempotent. */
  readonly start: () => void;
  /** Stops pinging; a ping in flight is ignored when it lands. Idempotent. */
  readonly stop: () => void;
}

type PingResult = 'answered' | 'missed' | 'inconclusive';

export function createWatchdog(options: WatchdogOptions): Watchdog {
  const intervalMs = options.intervalMs ?? PING_INTERVAL_MS;
  const timeoutMs = options.timeoutMs ?? PING_TIMEOUT_MS;
  const lateToleranceMs = options.lateToleranceMs ?? LATE_TOLERANCE_MS;
  const now = options.now ?? ((): number => performance.now());

  let running = false;
  /** Bumped by `stop()`, so a round that started before it cannot act after it. */
  let generation = 0;
  let timer: NodeJS.Timeout | null = null;
  let misses = 0;
  /** The gateway the current count belongs to. */
  let watched: CoreGateway | null = null;
  let lastAnswer: number | null = null;

  /** One ping, raced against a deadline the gateway cannot ignore. */
  async function ping(gateway: CoreGateway): Promise<PingResult> {
    const sent = now();
    let deadline: NodeJS.Timeout | undefined;
    const expired = new Promise<'deadline'>((resolve) => {
      deadline = setTimeout(() => {
        resolve('deadline');
      }, timeoutMs);
    });
    try {
      const response = await Promise.race([
        gateway.request({ method: 'GET', path: '/health' }),
        expired,
      ]);
      if (response !== 'deadline' && response.status === 200) return 'answered';
    } catch {
      // Refused, reset, timed out in the gateway: the core did not answer.
    } finally {
      clearTimeout(deadline);
    }
    // The core had its full window only if MAIN kept time. A deadline that
    // fired late means MAIN (or the whole machine) stopped, not the core.
    return now() - sent > timeoutMs + lateToleranceMs ? 'inconclusive' : 'missed';
  }

  function schedule(round: number, delayMs: number): void {
    if (!running || round !== generation) return;
    timer = setTimeout(
      () => {
        timer = null;
        void tick(round);
      },
      Math.max(0, delayMs),
    );
    timer.unref?.();
  }

  async function tick(round: number): Promise<void> {
    const started = now();
    const gateway = options.gateway();
    if (gateway !== watched) {
      watched = gateway;
      misses = 0;
      lastAnswer = null;
    }
    if (gateway === null || !options.taskRunning()) {
      misses = 0;
      schedule(round, intervalMs);
      return;
    }

    const result = await ping(gateway);
    if (!running || round !== generation) return;
    // The core may have been replaced while the ping was out; that answer
    // belongs to a process that is no longer the one being watched.
    if (options.gateway() !== gateway) {
      schedule(round, 0);
      return;
    }

    if (result === 'answered') {
      misses = 0;
      lastAnswer = now();
    } else if (result === 'inconclusive') {
      misses = 0;
    } else {
      misses += 1;
    }

    if (misses >= MISSES_TO_KILL && options.taskRunning()) {
      const report = { misses, silentMs: lastAnswer === null ? null : now() - lastAnswer };
      misses = 0;
      let outcome: WatchdogReport['outcome'];
      try {
        outcome = (await options.terminate()) ? 'terminated' : 'no-core';
      } catch {
        // The watchdog must outlive a failed kill: two more misses try again.
        outcome = 'failed';
      }
      options.onReport?.({ outcome, ...report });
    }
    schedule(round, started + intervalMs - now());
  }

  return {
    start: () => {
      if (running) return;
      running = true;
      misses = 0;
      watched = null;
      lastAnswer = null;
      schedule(generation, intervalMs);
    },
    stop: () => {
      running = false;
      generation += 1;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}
