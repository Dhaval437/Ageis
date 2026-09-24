/**
 * A stand-in core for `kill-switch.live.test.ts`: a real process tree that
 * speaks the real handshake, so the kill switch is exercised end to end against
 * something that can genuinely hang.
 *
 *   node fake-core.mts launcher <healthy|hung>
 *
 * The launcher re-runs this file as the core and waits on it — the same shape as
 * the venv's `python.exe`, which makes the process holding the mouse MAIN's
 * *grandchild*. The core reads the token from stdin, serves `/v1/health`, and
 * then either acknowledges `POST /v1/kill` (`healthy`) or wedges its own event
 * loop on it (`hung`), which is what a hung core looks like from MAIN: a port
 * that accepts and never answers.
 *
 * Run by Node directly (type stripping), never bundled or packaged.
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';

/** However a test fails, nothing here outlives it by long. */
const LIFETIME_MS = 30_000;
const bornAt = Date.now();

const [role, mode] = process.argv.slice(2);

function launcher(): void {
  const core = spawn(process.execPath, [fileURLToPath(import.meta.url), 'core', mode ?? ''], {
    stdio: 'inherit',
  });
  core.on('exit', (code) => process.exit(code ?? 1));
}

function readToken(): Promise<string> {
  return new Promise((resolve) => {
    let buffer = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk: string) => {
      buffer += chunk;
      const newline = buffer.indexOf('\n');
      if (newline !== -1) resolve(buffer.slice(0, newline));
    });
  });
}

/** What a hung core does: holds the thread and answers nothing, ever. */
function wedge(): never {
  while (Date.now() - bornAt < LIFETIME_MS) {
    // Spin. No timer, no socket, no signal handler runs from here on.
  }
  process.exit(3);
}

async function core(): Promise<void> {
  const token = await readToken();
  const server = createServer((request, response) => {
    if (request.headers.authorization !== `Bearer ${token}`) {
      response.writeHead(401).end();
      return;
    }
    const reply = (body: unknown): void => {
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(body));
    };
    if (request.method === 'GET' && request.url === '/v1/health') {
      reply({ status: 'ok', version: '0.0.0-test', uptime: 0 });
      return;
    }
    if (request.method === 'POST' && request.url === '/v1/kill') {
      if (mode === 'hung') wedge();
      reply({ engaged: true, released_keys: 0, released_buttons: 0, release_failures: 0 });
      return;
    }
    response.writeHead(404).end();
  });
  server.listen(0, '127.0.0.1', () => {
    const address = server.address();
    const port = typeof address === 'object' && address !== null ? address.port : 0;
    process.stdout.write(`${JSON.stringify({ port, pid: process.pid, version: '0.0.0-test' })}\n`);
  });

  // The fake's own parent watch: MAIN's `stop()` kills the launcher only.
  const launcherPid = process.ppid;
  setInterval(() => {
    try {
      process.kill(launcherPid, 0);
    } catch {
      process.exit(0);
    }
    if (Date.now() - bornAt > LIFETIME_MS) process.exit(0);
  }, 100);
}

if (role === 'launcher') launcher();
else if (role === 'core') void core();
else process.exit(2);
