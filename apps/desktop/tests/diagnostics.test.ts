import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  buildDiagnosticReport,
  coreLogFile,
  readLogTail,
  redactReportLine,
  REPORT_LOG_LINES,
  type DiagnosticInput,
} from '../src/main/diagnostics.js';
import { aegisLogDir } from '../src/main/paths.js';

/**
 * `RECOVERY.md § 4`: the report the user pastes into a bug report carries no
 * path from their home directory and nothing key-shaped. These are the tests
 * that fail if either leaks.
 */

const HOME = 'C:\\Users\\ada';

describe('redactReportLine', () => {
  it('removes a path under the user home, with either separator', () => {
    expect(redactReportLine('opening C:\\Users\\ada\\Documents\\taxes.xlsx now', HOME)).toBe(
      'opening %USERPROFILE%\\… now',
    );
    expect(redactReportLine('opening C:/Users/ada/Documents/taxes.xlsx now', HOME)).toBe(
      'opening %USERPROFILE%\\… now',
    );
  });

  it('removes a home path escaped inside a JSON log line', () => {
    const line =
      '{"msg":"storage.ready","db":"C:\\\\Users\\\\ada\\\\AppData\\\\Local\\\\aegis.db"}';
    const redacted = redactReportLine(line, HOME);
    expect(redacted).not.toContain('ada');
    expect(redacted).not.toContain('aegis.db');
    expect(redacted).toContain('storage.ready');
  });

  it('keeps paths that are not under the home directory', () => {
    const line = 'reading C:\\Windows\\System32\\drivers\\etc\\hosts';
    expect(redactReportLine(line, HOME)).toBe(line);
  });

  it('removes the user name on its own', () => {
    expect(redactReportLine('{"user":"ada"}', HOME)).toBe('{"user":"%USERNAME%"}');
  });

  it.each([
    ['{"token": "9f8e7d6c5b4a39281706f5e4d3c2b1a0"}', '9f8e7d6c5b4a39281706f5e4d3c2b1a0'],
    ['authorization: Bearer abc123', 'abc123'],
    ['--api-key=sk-live-000111222333', 'sk-live-000111222333'],
    ['password = hunter2', 'hunter2'],
  ])('removes the secret in %s', (line, secret) => {
    const redacted = redactReportLine(line, HOME);
    expect(redacted).not.toContain(secret);
    expect(redacted).toContain('[redacted]');
  });

  it('removes an unlabelled 64-character token', () => {
    const token = 'a'.repeat(32) + 'b'.repeat(32);
    expect(redactReportLine(`handshake ${token} done`, HOME)).toBe('handshake [redacted] done');
  });

  it('truncates a very long line instead of copying it whole', () => {
    const redacted = redactReportLine('word '.repeat(1000), HOME);
    expect(redacted.length).toBeLessThan(600);
    expect(redacted).toContain('(truncated)');
  });
});

describe('readLogTail', () => {
  let dir = '';

  beforeAll(async () => {
    dir = await mkdtemp(join(tmpdir(), 'aegis-diag-'));
  });

  afterAll(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it('is empty when the core never wrote a log', async () => {
    await expect(readLogTail(join(dir, 'missing.log'), 200)).resolves.toEqual([]);
  });

  it('returns only the last lines, oldest first', async () => {
    const file = join(dir, 'core.log');
    await writeFile(file, Array.from({ length: 500 }, (_, i) => `line ${String(i)}`).join('\n'));
    const lines = await readLogTail(file, REPORT_LOG_LINES);
    expect(lines).toHaveLength(REPORT_LOG_LINES);
    expect(lines[0]).toBe('line 300');
    expect(lines.at(-1)).toBe('line 499');
  });

  it('reads only the tail of a log larger than the cap, and drops the partial line', async () => {
    const file = join(dir, 'big.log');
    const filler = Array.from({ length: 40_000 }, (_, i) => `filler ${String(i)}`).join('\n');
    await writeFile(file, `${filler}\nlast line`);
    const lines = await readLogTail(file, 5);
    expect(lines.at(-1)).toBe('last line');
    expect(lines).toHaveLength(5);
  });
});

describe('buildDiagnosticReport', () => {
  const input: DiagnosticInput = {
    versions: {
      app: '0.1.0',
      electron: '32.0.0',
      chrome: '128.0.0',
      node: '20.16.0',
      os: 'win32 10.0.26100 (x64)',
    },
    engine: {
      status: 'unavailable',
      attempts: 3,
      lastError: 'spawn C:\\Users\\ada\\AppData\\Local\\Aegis\\core.exe ENOENT',
    },
    logLines: ['{"msg":"core.started","token":"deadbeefdeadbeefdeadbeefdeadbeef"}'],
    homeDir: HOME,
    generatedAt: new Date('2026-09-16T10:00:00.000Z'),
  };

  it('reports the versions and the engine state', () => {
    const report = buildDiagnosticReport(input);
    expect(report).toContain('App: 0.1.0');
    expect(report).toContain('Electron: 32.0.0');
    expect(report).toContain('win32 10.0.26100 (x64)');
    expect(report).toContain('Engine: unavailable after 3 attempts');
    expect(report).toContain('2026-09-16T10:00:00.000Z');
  });

  it('redacts the engine error and every log line', () => {
    const report = buildDiagnosticReport(input);
    expect(report).not.toContain('ada');
    expect(report).not.toContain('deadbeef');
    expect(report).toContain('ENOENT');
    expect(report).toContain('core.started');
  });

  it('says so when there is no log file, rather than looking empty', () => {
    expect(buildDiagnosticReport({ ...input, logLines: [] })).toContain(
      'No engine log file was found.',
    );
  });

  it('records no error when the supervisor has not seen one', () => {
    expect(
      buildDiagnosticReport({ ...input, engine: { ...input.engine, lastError: null } }),
    ).toContain('Last error: none recorded');
  });
});

describe('aegisLogDir', () => {
  it('follows the core: %LOCALAPPDATA%\\Aegis\\logs', () => {
    expect(aegisLogDir({ LOCALAPPDATA: 'C:\\Users\\ada\\AppData\\Local' }, HOME)).toBe(
      join('C:\\Users\\ada\\AppData\\Local', 'Aegis', 'logs'),
    );
  });

  it('falls back to ~/.aegis/logs, as the core does', () => {
    expect(aegisLogDir({}, HOME)).toBe(join(HOME, '.aegis', 'logs'));
  });

  it('names the same file the core writes', () => {
    expect(coreLogFile('L')).toBe(join('L', 'core.log'));
  });
});
