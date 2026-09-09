import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  HandshakeError,
  createSessionToken,
  parseHandshakeLine,
  startCore,
  type CoreLaunchSpec,
} from '../src/main/handshake.js';

/**
 * A stand-in for the spawned core: a real pair of streams so the reader is
 * exercised for real, plus a record of what MAIN wrote to stdin — which is the
 * one place the session token is allowed to go.
 */
class FakeCore extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly stdin = new PassThrough();
  readonly killed: string[] = [];
  received = '';

  constructor() {
    super();
    this.stdin.on('data', (chunk: Buffer) => {
      this.received += chunk.toString('utf8');
    });
  }

  kill(signal?: string): boolean {
    this.killed.push(signal ?? 'SIGTERM');
    return true;
  }

  announce(line: string): void {
    this.stdout.write(line);
  }
}

const SPEC: CoreLaunchSpec = { command: 'aegis-core.exe', args: ['--port', '0'] };

function withCore(): { core: FakeCore; spawn: (spec: CoreLaunchSpec) => FakeCore } {
  const core = new FakeCore();
  return { core, spawn: () => core };
}

function start(core: FakeCore, timeoutMs = 500) {
  // The fake satisfies the parts of the child-process surface this module uses.
  return startCore(SPEC, {
    spawn: (() => core) as never,
    timeoutMs,
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('createSessionToken', () => {
  it('is 256 bits, hex encoded', () => {
    expect(createSessionToken()).toMatch(/^[0-9a-f]{64}$/);
  });

  it('is different every time, so a respawn never reuses a credential', () => {
    const tokens = new Set(Array.from({ length: 50 }, () => createSessionToken()));
    expect(tokens.size).toBe(50);
  });
});

describe('parseHandshakeLine', () => {
  it('accepts the line the core documents', () => {
    expect(parseHandshakeLine('{"port":54321,"pid":900,"version":"0.1.0"}')).toEqual({
      port: 54321,
      pid: 900,
      version: '0.1.0',
    });
  });

  it('ignores fields it was not promised', () => {
    expect(parseHandshakeLine('{"port":1,"pid":2,"version":"3","extra":"x"}')).toEqual({
      port: 1,
      pid: 2,
      version: '3',
    });
  });

  it.each([
    ['not json', 'hello'],
    ['an array', '[1,2,3]'],
    ['null', 'null'],
    ['a missing port', '{"pid":1,"version":"0"}'],
    ['a string port', '{"port":"54321","pid":1,"version":"0"}'],
    ['port 0, which is never a bound port', '{"port":0,"pid":1,"version":"0"}'],
    ['a port above the range', '{"port":70000,"pid":1,"version":"0"}'],
    ['a fractional port', '{"port":1.5,"pid":1,"version":"0"}'],
    ['a missing pid', '{"port":1,"version":"0"}'],
    ['pid 0', '{"port":1,"pid":0,"version":"0"}'],
    ['an empty version', '{"port":1,"pid":1,"version":""}'],
    ['an absurd version', `{"port":1,"pid":1,"version":"${'v'.repeat(500)}"}`],
  ])('rejects %s', (_label, line) => {
    expect(parseHandshakeLine(line)).toBeNull();
  });
});

describe('startCore', () => {
  it('resolves with what the core announced', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('{"port":54321,"pid":900,"version":"0.1.0"}\n');

    const { session } = await started;
    expect(session.port).toBe(54321);
    expect(session.pid).toBe(900);
    expect(session.version).toBe('0.1.0');
  });

  it('sends the token over the stdin pipe and closes it', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('{"port":1,"pid":2,"version":"3"}\n');
    const { session } = await started;

    expect(core.received).toBe(`${session.token}\n`);
    expect(session.token).toMatch(/^[0-9a-f]{64}$/);
  });

  it('tells the core which PID supervises it', async () => {
    // The core cannot read this off its own parent: a launcher in between makes
    // MAIN a grandparent, and the peer check would then refuse MAIN itself.
    const core = new FakeCore();
    let spawnedWith: CoreLaunchSpec | null = null;
    const started = startCore(SPEC, {
      spawn: ((spec: CoreLaunchSpec) => {
        spawnedWith = spec;
        return core;
      }) as never,
      timeoutMs: 500,
    });
    core.announce('{"port":1,"pid":2,"version":"3"}\n');
    await started;

    const args = (spawnedWith as CoreLaunchSpec | null)?.args ?? [];
    expect(args).toEqual(['--port', '0', '--supervisor-pid', String(process.pid)]);
  });

  it('never puts the token on the command line', async () => {
    // argv is world-readable through WMI (ARCHITECTURE.md § 3.1 step 2).
    const core = new FakeCore();
    let spawnedWith: CoreLaunchSpec | null = null;
    const started = startCore(SPEC, {
      spawn: ((spec: CoreLaunchSpec) => {
        spawnedWith = spec;
        return core;
      }) as never,
      timeoutMs: 500,
    });
    core.announce('{"port":1,"pid":2,"version":"3"}\n');
    const { session } = await started;

    const spec = spawnedWith as CoreLaunchSpec | null;
    expect(spec).not.toBeNull();
    expect(JSON.stringify(spec)).not.toContain(session.token);
  });

  it('handles a line that arrives split across chunks', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('{"port":543');
    core.announce('21,"pid":900,"ver');
    core.announce('sion":"0.1.0"}\n');

    expect((await started).session.port).toBe(54321);
  });

  it('ignores anything the core writes after the handshake line', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('{"port":1,"pid":2,"version":"3"}\nnoise\nmore noise\n');

    expect((await started).session.port).toBe(1);
  });

  it('rejects and kills the core when the line is malformed', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('this is not the handshake\n');

    await expect(started).rejects.toBeInstanceOf(HandshakeError);
    expect(core.killed.length).toBe(1);
  });

  it('rejects when the core floods stdout instead of answering', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('x'.repeat(9000));

    await expect(started).rejects.toThrow(/too much/);
    expect(core.killed.length).toBe(1);
  });

  it('rejects when the core closes stdout without announcing', async () => {
    const { core } = withCore();
    const started = start(core);
    core.stdout.end();

    await expect(started).rejects.toThrow(/closed its output/);
  });

  it('rejects when the core exits during startup', async () => {
    const { core } = withCore();
    const started = start(core);
    core.emit('exit', 2, null);

    await expect(started).rejects.toThrow(/exited during startup \(code 2\)/);
  });

  it('rejects when the core cannot be spawned at all', async () => {
    const { core } = withCore();
    const started = start(core);
    core.emit('error', new Error('ENOENT'));

    await expect(started).rejects.toThrow(/could not be started/);
  });

  it('rejects and kills the core when it never answers', async () => {
    const { core } = withCore();
    const started = start(core, 20);

    await expect(started).rejects.toThrow(/did not answer/);
    expect(core.killed.length).toBe(1);
  });

  it('kills the core exactly once even if it exits after a failure', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('garbage\n');
    await expect(started).rejects.toThrow();

    core.emit('exit', 1, null);
    core.stdout.end();
    expect(core.killed.length).toBe(1);
  });

  it('does not kill a core that handshook correctly', async () => {
    const { core } = withCore();
    const started = start(core);
    core.announce('{"port":1,"pid":2,"version":"3"}\n');
    await started;

    expect(core.killed).toEqual([]);
  });
});
